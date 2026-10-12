"""显式读取模型配置，不搜索其他项目，也不将密钥写入任务记录。"""
from dataclasses import dataclass, field
import os
from pathlib import Path


@dataclass(frozen=True)
class ModelSettings:
    model: str
    api_key: str = field(repr=False)

    @classmethod
    def load(cls, env_file: Path | None = None, model: str | None = None):
        values = {}
        if env_file is not None:
            from dotenv import dotenv_values
            if not env_file.is_file():
                raise ValueError('指定的模型配置文件不存在')
            values = dotenv_values(env_file)
        key = os.getenv('DASHSCOPE_API_KEY') or values.get('DASHSCOPE_API_KEY')
        if not key:
            raise ValueError('需要 DASHSCOPE_API_KEY；请通过环境变量或 --env-file 配置')
        name = (model or os.getenv('REPAIR_MODEL') or values.get('REPAIR_MODEL')
                or values.get('STUDY_MODEL') or 'qwen-plus')
        return cls(model=name, api_key=key)

    def create_model(self):
        from langchain_community.chat_models import ChatTongyi
        return ChatTongyi(model=self.model, api_key=self.api_key, max_retries=0,
                          model_kwargs={'timeout': 30, 'max_tokens': 4096}, cache=False)
