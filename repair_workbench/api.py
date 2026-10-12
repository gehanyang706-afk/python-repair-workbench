"""本机工作台 HTTP API；任务状态与 SSE 事件来自持久化队列。"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .service import TaskService
from .storage import TERMINAL


class RepositoryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    repo: str = Field(min_length=1, max_length=4096)
    revision: str = Field(default='HEAD', min_length=1, max_length=200)


class TaskRequest(RepositoryRequest):
    task: str = Field(min_length=1, max_length=12000)
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_model_calls: int = Field(default=12, ge=1, le=30)
    deadline: float = Field(default=180, ge=10, le=600, allow_inf_nan=False)
    test_timeout: float = Field(default=30, ge=1, le=60, allow_inf_nan=False)


def create_app(service: TaskService, *, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            service.start()
        try:
            yield
        finally:
            if start_worker:
                await asyncio.to_thread(service.stop)

    app = FastAPI(title='Python Repair Workbench', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])

    @app.middleware('http')
    async def local_requests(request: Request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            expected = f'{request.url.scheme}://{request.url.netloc}'
            if request.headers.get('x-workbench-request') != '1' or (origin and origin != expected):
                return JSONResponse({'detail': '请求来源无效'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(subprocess.SubprocessError)
    async def process_error(request, exc):
        return JSONResponse({'detail': '无法读取 Git 仓库或执行本地工具，请检查路径与版本'}, status_code=400)

    @app.exception_handler(OSError)
    async def file_error(request, exc):
        return JSONResponse({'detail': '路径不可用或文件尚未生成'}, status_code=404)

    def get_task(task_id):
        task = service.store.get(task_id)
        if task is None:
            raise HTTPException(404, '任务不存在')
        return task

    @app.get('/api/config')
    def config():
        return {'repo_roots': [str(p) for p in service.repo_roots], 'image': service.image,
                'model': service.model_name, 'model_ready': service.model_factory is not None}

    @app.post('/api/repository')
    def inspect_repository(body: RepositoryRequest):
        return service.repository(body.repo, body.revision)

    @app.post('/api/tasks', status_code=201)
    def submit(body: TaskRequest):
        return service.submit(body.model_dump())

    @app.get('/api/tasks')
    def tasks():
        return service.store.list()

    @app.get('/api/tasks/{task_id}')
    def detail(task_id: str):
        return get_task(task_id)

    @app.post('/api/tasks/{task_id}/cancel')
    def cancel(task_id: str):
        get_task(task_id)
        service.store.cancel(task_id)
        return get_task(task_id)

    @app.get('/api/tasks/{task_id}/events')
    async def events(task_id: str, request: Request, after: int = Query(0, ge=0), follow: bool = True):
        get_task(task_id)
        header = request.headers.get('last-event-id')
        if header is not None:
            try:
                after = max(after, int(header))
            except ValueError:
                raise HTTPException(400, '事件游标无效') from None

        async def stream():
            cursor = after
            while True:
                batch = await asyncio.to_thread(service.store.events, task_id, cursor)
                for item in batch:
                    cursor = item['id']
                    yield f"id: {cursor}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if len(batch) == 100:
                    continue
                task = await asyncio.to_thread(service.store.get, task_id)
                if not follow or task['state'] in TERMINAL:
                    # 终态与事件在同一事务保存，再读一次以免遗漏最后一次提交。
                    tail = await asyncio.to_thread(service.store.events, task_id, cursor)
                    for item in tail:
                        yield f"id: {item['id']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    yield 'event: complete\ndata: {}\n\n'
                    break
                if await request.is_disconnected():
                    break
                yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/tasks/{task_id}/patch')
    def patch(task_id: str):
        return FileResponse(service.artifact(task_id, 'patch'), media_type='text/plain', filename=f'repair-{task_id[:8]}.diff')

    @app.get('/api/tasks/{task_id}/report')
    def report(task_id: str):
        return JSONResponse(get_task(task_id), headers={'Content-Disposition': f'attachment; filename="report-{task_id[:8]}.json"'})

    @app.get('/api/tasks/{task_id}/log')
    def log(task_id: str, attempt: int = Query(0, ge=0, le=5), offset: int = Query(0, ge=0)):
        path = service.artifact(task_id, 'log', attempt)
        with path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(64001)
        return {'content': data[:64000].decode('utf-8', errors='replace'),
                'next_offset': offset + 64000 if len(data) > 64000 else None}

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    return app
