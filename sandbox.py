"""
沙箱执行 — 在子进程中安全运行 Python 代码
"""

import os
import subprocess
import tempfile


def sandbox_exec(code: str, timeout: int = 10) -> tuple:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["python", tmp_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout[:2000] if result.stdout else "(无输出)"
        else:
            err = result.stderr[:2000] if result.stderr else f"退出码: {result.returncode}"
            return False, err
    except subprocess.TimeoutExpired:
        return False, f"超时 ({timeout}s)"
    except Exception as e:
        return False, f"执行异常: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
