from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
BASE_PATH = Path(__file__).resolve().parent.parent.parent

class AppBaseConfig(BaseSettings):
    """
    基础配置类，只提供从 .env 文件读取配置的功能
    
    其他配置都应该基于这个类来扩展
    """

    model_config = SettingsConfigDict(env_file=BASE_PATH / ".env", env_file_encoding="utf-8", frozen=True,
                                      extra="ignore", )

    @property
    def APP_BASE_DIR(self) -> Path:
        """项目根目录"""
        return BASE_PATH


if __name__ == "__main__":
    print(BASE_PATH / ".env")
    print(AppBaseConfig().model_dump())
