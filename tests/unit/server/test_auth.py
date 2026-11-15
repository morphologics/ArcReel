"""
Authentication core module tests

Tests password generation, JWT tokens, credential checks, etc. in server.auth.

认证核心模块测试

测试 server.auth 中的密码生成、JWT token、凭据校验等功能。
"""

import os
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import server.auth as auth_module


class TestGeneratePassword:
    def test_generate_password(self):
        """Default length 16, alphanumeric only

        默认长度 16，仅字母数字"""
        pwd = auth_module.generate_password()
        assert len(pwd) == 16
        assert pwd.isalnum()

    def test_generate_password_custom_length(self):
        """Custom length

        自定义长度"""
        pwd = auth_module.generate_password(length=32)
        assert len(pwd) == 32
        assert pwd.isalnum()


class TestTokenSecret:
    def setup_method(self):
        """Reset the cache before each test

        每个测试前重置缓存"""
        auth_module._cached_token_secret = None

    def test_get_token_secret_from_env(self):
        """The AUTH_TOKEN_SECRET env var takes precedence

        优先使用 AUTH_TOKEN_SECRET 环境变量"""
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "my-secret-key"}):
            secret = auth_module.get_token_secret()
            assert secret == "my-secret-key"

    def test_get_token_secret_auto_generate(self):
        """Generated automatically and cached

        自动生成并缓存"""
        env = os.environ.copy()
        env.pop("AUTH_TOKEN_SECRET", None)
        with patch.dict(os.environ, env, clear=True):
            secret1 = auth_module.get_token_secret()
            assert secret1 is not None
            assert len(secret1) > 0

            # Calling again should return the same cached value / 再次调用应返回相同的缓存值
            secret2 = auth_module.get_token_secret()
            assert secret1 == secret2


def _simulate_restart() -> None:
    """Process restart: all in-process caches are lost, the data directory and .env are kept.

    进程重启：进程内缓存全部丢失，数据目录与 .env 保留。"""
    auth_module.reset_token_secret_for_tests()
    auth_module._cached_password_hash = None


class TestTokenSecretPersistence:
    """When AUTH_TOKEN_SECRET is not set, the signing key lands in the data directory and is reused after restart.

    未设置 AUTH_TOKEN_SECRET 时签名密钥落在数据目录，重启后沿用。"""

    def setup_method(self):
        _simulate_restart()

    def teardown_method(self):
        _simulate_restart()

    @pytest.mark.parametrize("unset", ["missing", "empty"])
    def test_token_survives_restart_without_configured_secret(self, monkeypatch, unset):
        if unset == "missing":
            monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
        else:
            monkeypatch.setenv("AUTH_TOKEN_SECRET", "")
        token = auth_module.create_token("admin")

        _simulate_restart()

        payload = auth_module.verify_token(token)
        assert payload is not None
        assert payload["sub"] == "admin"

    def test_persisted_secret_file_is_owner_only(self, monkeypatch):
        from lib.infra.data_root_layout import DataRootLayout

        monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
        auth_module.ensure_token_secret()

        secret_path = DataRootLayout.current().auth_token_secret_path
        assert secret_path.read_text(encoding="utf-8").strip() == auth_module.get_token_secret()
        if os.name == "posix":
            assert secret_path.stat().st_mode & 0o777 == 0o600

    def test_existing_secret_file_is_reused(self, monkeypatch):
        from lib.infra.data_root_layout import DataRootLayout

        monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
        secret_path = DataRootLayout.current().auth_token_secret_path
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text("previously-persisted-secret\n", encoding="utf-8")

        assert auth_module.get_token_secret() == "previously-persisted-secret"

    def test_configured_secret_wins_over_persisted_file(self, monkeypatch):
        monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
        token_signed_with_persisted = auth_module.create_token("admin")

        _simulate_restart()
        monkeypatch.setenv("AUTH_TOKEN_SECRET", "explicit-secret-that-is-at-least-32-bytes")

        assert auth_module.get_token_secret() == "explicit-secret-that-is-at-least-32-bytes"
        assert auth_module.verify_token(token_signed_with_persisted) is None

    def test_unwritable_data_dir_falls_back_to_process_secret(self, monkeypatch, tmp_path, caplog):
        """When the key cannot be written to the data directory, startup is not blocked and falls back to an in-process random key (re-login needed after restart).

        数据目录写不进密钥时不阻断启动，退回进程内随机密钥（重启后需重新登录）。"""
        from lib.infra.app_data_dir import reset_for_tests

        monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
        data_root = tmp_path / "data-root"
        data_root.mkdir()
        (data_root / "runtime").write_text("", encoding="utf-8")  # runtime/ cannot be created / runtime/ 建不出来
        monkeypatch.setenv("ARCREEL_DATA_DIR", str(data_root))
        reset_for_tests()

        with caplog.at_level("WARNING", logger=auth_module.__name__):
            secret = auth_module.get_token_secret()

        assert secret
        assert auth_module.get_token_secret() == secret
        assert any("AUTH_TOKEN_SECRET" in r.getMessage() for r in caplog.records)
        assert all(secret not in r.getMessage() for r in caplog.records)


class TestCreateAndVerifyToken:
    def setup_method(self):
        auth_module._cached_token_secret = None

    def test_create_and_verify_token(self):
        """Round trip of create then verify is consistent

        创建后验证往返一致"""
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            token = auth_module.create_token("admin")
            payload = auth_module.verify_token(token)
            assert payload is not None
            assert payload["sub"] == "admin"
            assert "iat" in payload
            assert "exp" in payload

    def test_create_token_accepts_a_shorter_expiry(self):
        """The embedded Agent can issue short-lived session JWTs without changing the default lifetime of web logins.

        内嵌 Agent 可签发短时效会话 JWT，不改变网页登录默认时效。"""
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            token = auth_module.create_token("embedded-agent", expiry_seconds=900)
            payload = auth_module.verify_token(token)

        assert payload is not None
        assert payload["exp"] - payload["iat"] == 900

    def test_verify_token_invalid(self):
        """An invalid token returns None

        无效 token 返回 None"""
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            result = auth_module.verify_token("this-is-not-a-valid-jwt")
            assert result is None

    def test_verify_token_expired(self):
        """An expired token returns None

        过期 token 返回 None"""
        import jwt

        secret = "test-secret-key-that-is-at-least-32-bytes"
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": secret}):
            # Manually create an already expired token / 手动创建一个已过期的 token
            payload = {
                "sub": "admin",
                "iat": time.time() - 3600,
                "exp": time.time() - 1,  # Already expired / 已过期
            }
            expired_token = jwt.encode(payload, secret, algorithm="HS256")
            result = auth_module.verify_token(expired_token)
            assert result is None

    @pytest.mark.parametrize("purpose", ["download", "other"])
    def test_verify_token_rejects_purpose_bound_token(self, purpose):
        """Tokens with a purpose claim (any value) are not accepted by session verification

        带 purpose 声明的 token（任意取值）不被会话校验接受"""
        import jwt

        secret = "test-secret-key-that-is-at-least-32-bytes"
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": secret}):
            payload = {"sub": "admin", "purpose": purpose, "iat": time.time(), "exp": time.time() + 300}
            token = jwt.encode(payload, secret, algorithm="HS256")
            assert auth_module.verify_token(token) is None

    def test_verify_token_accepts_session_token(self):
        """Session tokens without purpose (both login and the embedded Agent issue via create_token) pass as usual

        不带 purpose 的会话 token（登录与内嵌 Agent 均经 create_token 签发）照常通过"""
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            payload = auth_module.verify_token(auth_module.create_token("embedded-agent", expiry_seconds=60))
            assert payload is not None
            assert payload["sub"] == "embedded-agent"


class TestCheckCredentials:
    def setup_method(self):
        auth_module._cached_password_hash = None

    def test_check_credentials_valid(self):
        """Correct credentials return True

        正确凭据返回 True"""
        with patch.dict(os.environ, {"AUTH_USERNAME": "admin", "AUTH_PASSWORD": "pass123"}):
            assert auth_module.check_credentials("admin", "pass123") is True

    def test_check_credentials_invalid(self):
        """Wrong credentials return False

        错误凭据返回 False"""
        with patch.dict(os.environ, {"AUTH_USERNAME": "admin", "AUTH_PASSWORD": "pass123"}):
            assert auth_module.check_credentials("admin", "wrong") is False
            assert auth_module.check_credentials("nobody", "pass123") is False

    def test_check_credentials_default_username(self):
        """When AUTH_USERNAME is not set it defaults to admin

        AUTH_USERNAME 未设置时默认为 admin"""
        env = os.environ.copy()
        env.pop("AUTH_USERNAME", None)
        env["AUTH_PASSWORD"] = "secret"
        with patch.dict(os.environ, env, clear=True):
            assert auth_module.check_credentials("admin", "secret") is True


class TestEnsureAuthPassword:
    def setup_method(self):
        """Reset the cache before each test

        每个测试前重置缓存"""
        auth_module._cached_token_secret = None

    def test_existing_password_returned(self):
        """When AUTH_PASSWORD already exists, return the existing password directly

        AUTH_PASSWORD 已存在时直接返回现有密码"""
        with patch.dict(os.environ, {"AUTH_PASSWORD": "existing-pwd"}):
            result = auth_module.ensure_auth_password()
            assert result == "existing-pwd"

    def test_auto_generate_when_empty(self, tmp_path):
        """When AUTH_PASSWORD is empty, generate a password automatically and write it to os.environ

        AUTH_PASSWORD 为空时自动生成密码并写入 os.environ"""
        env = os.environ.copy()
        env.pop("AUTH_PASSWORD", None)
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_VAR=value\n")
        with patch.dict(os.environ, env, clear=True):
            result = auth_module.ensure_auth_password(env_path=str(env_file))
            assert len(result) == 16
            assert result.isalnum()
            # It should also be written to os.environ / 应该同步写入 os.environ
            assert os.environ["AUTH_PASSWORD"] == result

    def test_writeback_replace_existing_line(self, tmp_path):
        """Write back to the .env file: replace the existing AUTH_PASSWORD= line

        回写 .env 文件：替换已有 AUTH_PASSWORD= 行"""
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_VAR=hello\nAUTH_PASSWORD=\nOTHER=world\n")
        env = os.environ.copy()
        env.pop("AUTH_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            password = auth_module.ensure_auth_password(env_path=str(env_file))
            content = env_file.read_text()
            assert f"AUTH_PASSWORD={password}" in content
            # Other existing lines should be kept / 原有的其他行应保留
            assert "SOME_VAR=hello" in content
            assert "OTHER=world" in content

    def test_writeback_append_when_no_line(self, tmp_path):
        """Write back to the .env file: append when the file has no AUTH_PASSWORD line

        回写 .env 文件：追加，原文件无 AUTH_PASSWORD 行"""
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_VAR=hello\n")
        env = os.environ.copy()
        env.pop("AUTH_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            password = auth_module.ensure_auth_password(env_path=str(env_file))
            content = env_file.read_text()
            assert f"AUTH_PASSWORD={password}" in content
            # Existing content should be kept / 原有内容应保留
            assert "SOME_VAR=hello" in content

    def test_env_file_not_exist_no_error(self, tmp_path):
        """No exception when the .env file does not exist, and a new file is created

        .env 文件不存在时不抛异常，并创建新文件"""
        env_file = tmp_path / "nonexistent" / ".env"
        env = os.environ.copy()
        env.pop("AUTH_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            # A missing parent directory triggers OSError; the function should not raise / 父目录不存在会触发 OSError，函数不应抛异常
            password = auth_module.ensure_auth_password(env_path=str(env_file))
            assert len(password) == 16
            assert password.isalnum()

    def test_env_file_non_utf8_does_not_block_startup(self, tmp_path):
        """When a legacy .env uses a local encoding such as cp936 / ANSI, ``read_text(encoding="utf-8")``
        raises ``UnicodeDecodeError``. The function is called during server lifespan startup, so it must catch
        it and degrade (not persist, keep the in-process password), otherwise the service cannot start.

        历史 .env 用 cp936 / ANSI 等本地编码时，``read_text(encoding="utf-8")``
        会抛 ``UnicodeDecodeError``。该函数被 server lifespan 启动期调用，必须捕获
        并降级（不持久化、保留进程内密码），否则服务无法启动。
        """
        env_file = tmp_path / ".env"
        # A cp936-encoded Chinese comment + an existing AUTH_PASSWORD; UTF-8 decoding is bound to fail
        # cp936 编码的中文注释 + 现有 AUTH_PASSWORD，UTF-8 解码必失败
        env_file.write_bytes("# 注释\nAUTH_PASSWORD=old\n".encode("cp936"))
        env = os.environ.copy()
        env.pop("AUTH_PASSWORD", None)
        with patch.dict(os.environ, env, clear=True):
            password = auth_module.ensure_auth_password(env_path=str(env_file))
            assert len(password) == 16
            assert password.isalnum()
            # .env content is not damaged (no forced overwrite, to avoid losing user content) / .env 内容未被破坏（不强制覆写避免丢用户内容）
            assert env_file.read_bytes() == "# 注释\nAUTH_PASSWORD=old\n".encode("cp936")


class TestPasswordAcrossRestarts:
    """When Docker restarts without recreating the container, the AUTH_PASSWORD injected by compose stays an empty string.

    Docker 重启不重建容器时，compose 注入的 AUTH_PASSWORD 一直是空串。"""

    def _restart(self, env_file, monkeypatch) -> str:
        from lib.infra.env_init import load_env_file

        _simulate_restart()
        monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)  # The root conftest turned off .env reading / 根 conftest 关掉了 .env 读取
        # The value fixed by the template `AUTH_PASSWORD=` when the container was created
        monkeypatch.setenv("AUTH_PASSWORD", "")  # 容器创建时由模板 `AUTH_PASSWORD=` 固定下来的值
        load_env_file(env_file)
        return auth_module.ensure_auth_password(env_path=str(env_file))

    def test_password_edited_in_env_file_is_kept(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("AUTH_USERNAME=admin\nAUTH_PASSWORD=my-new-pass\n", encoding="utf-8")

        assert self._restart(env_file, monkeypatch) == "my-new-pass"
        assert env_file.read_text(encoding="utf-8") == "AUTH_USERNAME=admin\nAUTH_PASSWORD=my-new-pass\n"
        assert auth_module.check_credentials("admin", "my-new-pass") is True

    def test_generated_once_on_first_start_then_stable(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("AUTH_USERNAME=admin\nAUTH_PASSWORD=\n", encoding="utf-8")

        generated = self._restart(env_file, monkeypatch)
        assert len(generated) == 16
        written = env_file.read_text(encoding="utf-8")
        assert f"AUTH_PASSWORD={generated}" in written

        assert self._restart(env_file, monkeypatch) == generated
        assert self._restart(env_file, monkeypatch) == generated
        assert env_file.read_text(encoding="utf-8") == written


class TestDownloadToken:
    def setup_method(self):
        auth_module._cached_token_secret = None

    def test_create_and_verify_download_token(self):
        """Issue and verify a download token

        签发并验证下载 token"""
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            token = auth_module.create_download_token("admin", "my-project")
            payload = auth_module.verify_download_token(token, "my-project")
            assert payload["sub"] == "admin"
            assert payload["project"] == "my-project"
            assert payload["purpose"] == "download"

    def test_verify_download_token_wrong_project(self):
        """A project mismatch should raise ValueError

        项目不匹配应抛出 ValueError"""
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            token = auth_module.create_download_token("admin", "project-a")
            import pytest

            with pytest.raises(ValueError, match="project 不匹配"):
                auth_module.verify_download_token(token, "project-b")

    def test_verify_download_token_expired(self):
        """An expired token should raise ExpiredSignatureError

        过期 token 应抛出 ExpiredSignatureError"""
        import jwt

        secret = "test-secret-key-that-is-at-least-32-bytes"
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": secret}):
            payload = {
                "sub": "admin",
                "project": "demo",
                "purpose": "download",
                "iat": time.time() - 600,
                "exp": time.time() - 1,
            }
            expired_token = jwt.encode(payload, secret, algorithm="HS256")
            with pytest.raises(jwt.ExpiredSignatureError):
                auth_module.verify_download_token(expired_token, "demo")

    def test_verify_download_token_wrong_purpose(self):
        """A purpose mismatch should raise ValueError

        purpose 不匹配应抛出 ValueError"""
        import jwt

        secret = "test-secret-key-that-is-at-least-32-bytes"
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": secret}):
            payload = {
                "sub": "admin",
                "project": "demo",
                "purpose": "other",
                "iat": time.time(),
                "exp": time.time() + 300,
            }
            token = jwt.encode(payload, secret, algorithm="HS256")
            with pytest.raises(ValueError, match="purpose 不匹配"):
                auth_module.verify_download_token(token, "demo")


class TestPasswordHash:
    """Password hashing tests

    密码哈希功能测试"""

    def setup_method(self):
        auth_module._cached_password_hash = None

    def test_check_credentials_wrong_password_with_hash(self):
        """Hash comparison fails for a wrong password

        错误密码哈希比对失败"""
        with patch.dict(os.environ, {"AUTH_USERNAME": "admin", "AUTH_PASSWORD": "pass123"}):
            assert auth_module.check_credentials("admin", "wrong") is False

    def test_check_credentials_wrong_username_timing_safe(self):
        """A wrong username also runs hash verification (guarding against timing attacks)

        错误用户名也执行哈希验证（防时序攻击）"""
        with patch.dict(os.environ, {"AUTH_USERNAME": "admin", "AUTH_PASSWORD": "pass123"}):
            assert auth_module.check_credentials("nobody", "pass123") is False

    def test_password_hash_cached(self):
        """The hash value should be cached

        哈希值应被缓存"""
        with patch.dict(os.environ, {"AUTH_USERNAME": "admin", "AUTH_PASSWORD": "pass123"}):
            auth_module.check_credentials("admin", "pass123")
            first_hash = auth_module._cached_password_hash
            auth_module.check_credentials("admin", "pass123")
            assert auth_module._cached_password_hash is first_hash  # Same object / 同一对象


class TestGetCurrentUser:
    """FastAPI dependency function tests

    FastAPI 依赖函数测试"""

    def setup_method(self):
        auth_module._cached_token_secret = None

    async def test_get_current_user_valid_token(self):
        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            token = auth_module.create_token("admin")
            result = await auth_module.get_current_user(token)
            assert isinstance(result, auth_module.CurrentUserInfo)
            assert result.sub == "admin"
            assert result.id == "default"
            assert result.role == "admin"

    async def test_get_current_user_invalid_token(self):

        with patch.dict(os.environ, {"AUTH_TOKEN_SECRET": "test-secret-key-that-is-at-least-32-bytes"}):
            with pytest.raises(HTTPException) as exc_info:
                await auth_module.get_current_user("invalid-token")
            assert exc_info.value.status_code == 401
