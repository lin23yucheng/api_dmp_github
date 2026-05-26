"""
数据库访问基础封装。
"""
import configparser
from pathlib import Path

try:
    import psycopg2
except ModuleNotFoundError:
    psycopg2 = None


class DbClient:
    def __init__(self, config_path=None):
        root_dir = Path(__file__).resolve().parents[1]
        self.config_path = Path(config_path) if config_path else root_dir / "config" / "env_config.ini"
        self.config = configparser.ConfigParser()
        self.config.read(self.config_path, encoding="utf-8")
        self.execution_env = self._get_execution_env()

    def _get_execution_env(self):
        env = self.config.get("environment", "execution_env", fallback="").strip().lower()
        if env not in {"fat", "prod"}:
            raise ValueError(f"execution_env 配置错误: {env}，仅支持 fat 或 prod")
        return env

    def _resolve_db_section(self, db_alias="PostgreSQL"):
        section = f"{self.execution_env}-{db_alias}"
        if not self.config.has_section(section):
            raise ValueError(f"未找到数据库配置节: {section}")
        return section

    def _get_conn_kwargs(self, db_alias="PostgreSQL"):
        section = self._resolve_db_section(db_alias=db_alias)
        return {
            "host": self.config.get(section, "host"),
            "port": self.config.getint(section, "port"),
            "user": self.config.get(section, "user_name"),
            "password": self.config.get(section, "password"),
            "dbname": self.config.get(section, "database"),
        }

    def _ensure_driver(self):
        if psycopg2 is None:
            raise ModuleNotFoundError("未安装 psycopg2，请先执行: pip install psycopg2-binary")

    def query_one(self, sql, params=None, db_alias="PostgreSQL"):
        rows = self.query_all(sql, params=params, db_alias=db_alias)
        return rows[0] if rows else None

    def query_all(self, sql, params=None, db_alias="PostgreSQL"):
        self._ensure_driver()
        conn_kwargs = self._get_conn_kwargs(db_alias=db_alias)
        with psycopg2.connect(**conn_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                return cur.fetchall()

    def query_all_dict(self, sql, params=None, db_alias="PostgreSQL"):
        self._ensure_driver()
        conn_kwargs = self._get_conn_kwargs(db_alias=db_alias)
        with psycopg2.connect(**conn_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                description = cur.description or []
                columns = [desc[0] for desc in description]
                rows = cur.fetchall()
        return [dict(zip(columns, row)) for row in rows]
