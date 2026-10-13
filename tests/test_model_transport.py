"""No provider calls: slow workers must die, release sockets and preserve stop reasons."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from test_agent import fixture
from test_agent import ScriptedModel
from repair_workbench.model_transport import ManagedTongyi,ProviderRequestError,run_request_worker
from repair_workbench.agent import run_agent
from repair_workbench.session import BudgetExceeded, RepairSession
from repair_workbench.settings import model_configuration, ModelSettings


WORKER = '''import json,os,signal,socket,sys,time
p=json.load(sys.stdin)
open(p['pid_file'],'w').write(str(os.getpid()))
if p.get('ignore_term'): signal.signal(signal.SIGTERM,signal.SIG_IGN)
if p.get('port'):
 s=socket.create_connection(('127.0.0.1',p['port']))
 s.recv(1)
time.sleep(p.get('delay',0))
open(p['late_file'],'w').write('completed')
print(json.dumps({'ok':True}))
'''


class SlowManagedModel(ManagedTongyi):
    command: list
    payload: dict

    def invoke_controlled(self,messages,session,stop=None,**kwargs):
        return run_request_worker(self.payload,session,self.call_timeout,command=self.command)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=fixture(self.root)
        self.worker=self.root/'worker.py';self.worker.write_text(WORKER)
        self.payload={'pid_file':str(self.root/'pid'),'late_file':str(self.root/'late'),'delay':20}
        self.command=[sys.executable,str(self.worker)]

    def session(self,**kwargs):
        return RepairSession(self.base,self.root/'agent',{'pricing.py'},**kwargs)

    def assert_reaped(self,session):
        pid=int(Path(self.payload['pid_file']).read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        self.assertFalse(Path(self.payload['late_file']).exists())
        events=[json.loads(line) for line in (session.output/'events.jsonl').read_text().splitlines()]
        finished=[event for event in events if event['event']=='model_worker_finished']
        self.assertEqual(len(finished),1)
        self.assertTrue(finished[0]['cleanup_ok'])

    def test_wall_timeout_kills_and_reaps_blocking_worker(self):
        s=self.session();start=time.monotonic()
        with self.assertRaisesRegex(BudgetExceeded,'model_call_timeout'):
            run_request_worker(self.payload,s,.25,command=self.command)
        self.assertLess(time.monotonic()-start,1.5);self.assert_reaped(s)

    def test_cancel_and_task_deadline_are_distinct_from_call_timeout(self):
        for reason in ['cancel_requested','deadline_exceeded']:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);base=fixture(root);cancel=threading.Event()
                s=RepairSession(base,root/'agent',{'pricing.py'},deadline_seconds=.25 if reason=='deadline_exceeded' else 5,
                                cancellation_check=cancel.is_set)
                if reason=='cancel_requested':
                    timer=threading.Timer(.25,cancel.set);timer.start();self.addCleanup(timer.join)
                with self.assertRaisesRegex(BudgetExceeded,reason):
                    run_request_worker(self.payload,s,5,command=self.command)
                self.assert_reaped(s)

    def test_sigterm_resistance_escalates_to_kill(self):
        s=self.session()
        with self.assertRaisesRegex(BudgetExceeded,'model_call_timeout'):
            run_request_worker({**self.payload,'ignore_term':True},s,.25,command=self.command)
        self.assert_reaped(s)

    def test_cancel_closes_worker_owned_socket(self):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(2)
        self.addCleanup(listener.close);released=threading.Event()
        def server():
            connection,_=listener.accept()
            with connection:
                connection.settimeout(2)
                if connection.recv(1)==b'':released.set()
        thread=threading.Thread(target=server);thread.start();self.addCleanup(thread.join)
        s=self.session()
        with self.assertRaisesRegex(BudgetExceeded,'model_call_timeout'):
            run_request_worker({**self.payload,'port':listener.getsockname()[1]},s,.3,command=self.command)
        self.assertTrue(released.wait(.5));self.assert_reaped(s)

    def test_sdk_timeout_parameter_and_no_network_model_creation(self):
        from dashscope.api_entities.api_request_factory import _build_api_request
        args=dict(model='qwen-plus',input={'messages':[]},task_group='aigc',task='text-generation',
                  function='generation',api_key='offline-placeholder')
        self.assertEqual(_build_api_request(**args,timeout=30).timeout,300)
        self.assertEqual(_build_api_request(**args,request_timeout=(5,30)).timeout,(5,30))
        model=ModelSettings('qwen-plus','offline-placeholder').create_model()
        self.assertEqual(model.call_timeout,30)
        self.assertNotIn('offline-placeholder',repr(model))
        self.assertEqual(model_configuration()['sdk_connection_retries'],0)

    def test_agent_stop_records_reaped_request_and_unknown_usage(self):
        s=self.session()
        model=SlowManagedModel(api_key='offline-placeholder',command=self.command,
                               payload=self.payload,call_timeout=.25)
        result=run_agent(s,model,'fix pricing')
        self.assertEqual(result['stop_reason'],'model_call_timeout')
        self.assertEqual(result['model_calls'],1)
        self.assertFalse(result['usage_complete'])
        self.assertEqual(result['attempts'],[])
        self.assert_reaped(s)

    def test_worker_disables_sdk_hidden_retry_and_closes_pool_on_error(self):
        from repair_workbench.provider_worker import request
        import dashscope.api_entities.http_request as transport
        send=MagicMock(side_effect=ConnectionError('secret-provider-error'))
        fake=MagicMock();fake.invoke.side_effect=lambda *a,**k: transport._send_with_retry(send)
        payload={'model':'qwen-plus','api_key':'offline-placeholder','tools':[],
                 'messages':[],'stop':None,'connect_timeout':5,'read_timeout':30}
        with patch('langchain_community.chat_models.ChatTongyi',return_value=fake) as constructor, \
             patch.object(transport,'close_shared_sync_session') as close:
            with self.assertRaises(ConnectionError):request(payload)
        self.assertEqual(send.call_count,1);close.assert_called_once()
        self.assertEqual(constructor.call_args.kwargs['model_kwargs']['request_timeout'],(5,30))

    def test_cli_interrupt_reaps_worker_and_finishes_session(self):
        s=self.session()
        with patch.object(s,'check_budget',side_effect=[None,KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):run_request_worker(self.payload,s,5,command=self.command)
        events=[json.loads(line) for line in (s.output/'events.jsonl').read_text().splitlines()]
        event=next(event for event in events if event['event']=='model_worker_finished')
        self.assertTrue(event['cleanup_ok']);self.assertEqual(event['reason'],'cancel_requested')
        with self.assertRaises(ProcessLookupError):os.kill(event['pid'],0)
        with patch.object(ScriptedModel,'invoke',side_effect=KeyboardInterrupt()):
            result=run_agent(s,ScriptedModel(),'fix pricing')
        self.assertEqual(result['stop_reason'],'cancel_requested')
        self.assertEqual(result['state'],'stopped')

    def test_unconfirmed_cleanup_is_not_reported_as_cancelled(self):
        s=self.session();process=MagicMock();process.pid=424242;process.poll.return_value=None
        process.communicate.side_effect=__import__('subprocess').TimeoutExpired('offline-worker',1)
        process.wait.side_effect=__import__('subprocess').TimeoutExpired('offline-worker',1)
        with patch('repair_workbench.model_transport.subprocess.Popen',return_value=process), \
             patch('repair_workbench.model_transport.os.killpg'):
            with self.assertRaisesRegex(ProviderRequestError,'model_worker_cleanup_failed'):
                run_request_worker(self.payload,s,.01)
        events=[json.loads(line) for line in (s.output/'events.jsonl').read_text().splitlines()]
        self.assertFalse(events[-1]['cleanup_ok'])

    def test_cleanup_failure_keeps_executor_lock(self):
        from repair_workbench.service import TaskService
        first=TaskService(self.root/'guard-data',[self.root/'source'],'offline-image',model_factory=ScriptedModel)
        first.start();self.addCleanup(first.lock_file.close)
        first.request_cleanup_failed=True;first.stopping.set()
        with self.assertRaisesRegex(RuntimeError,'清理未确认'):first.stop()
        self.assertFalse(first.worker.is_alive())
        second=TaskService(self.root/'guard-data',[self.root/'source'],'offline-image',model_factory=ScriptedModel)
        with self.assertRaisesRegex(RuntimeError,'只能运行一个'):second.start()


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS')=='1','requires local Docker image')
class ServiceCancellationTests(unittest.TestCase):
    setUp=TransportTests.setUp
    def test_store_cancel_interrupts_inflight_request_and_preserves_evidence(self):
        from repair_workbench.service import TaskService
        model=SlowManagedModel(api_key='offline-placeholder',command=self.command,payload=self.payload,call_timeout=5)
        service=TaskService(self.root/'data',[self.root/'source'],
                            os.environ.get('REPAIR_TEST_IMAGE','study-agent-deepagents:0.1'),
                            model_factory=lambda:model,profile='simple_edit')
        task=service.submit({'repo':str(self.root/'source'),'task':'fix pricing','allowed_paths':['pricing.py'],
            'test_timeout':30,'deadline':30,'max_model_calls':12,'max_attempts':3})
        thread=threading.Thread(target=service.execute,args=(service.store.claim(),))
        thread.start();self.addCleanup(thread.join)
        expires=time.monotonic()+5
        while not Path(self.payload['pid_file']).exists() and time.monotonic()<expires:time.sleep(.01)
        self.assertTrue(Path(self.payload['pid_file']).exists())
        service.store.cancel(task['id']);thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        result=service.store.get(task['id'])
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(result['result']['agent']['stop_reason'],'cancel_requested')
        self.assertTrue(result['result']['baseline']['cleanup_ok'])
        pid=int(Path(self.payload['pid_file']).read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)
