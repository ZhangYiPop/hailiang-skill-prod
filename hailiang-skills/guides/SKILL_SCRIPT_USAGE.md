# Skill 脚本使用约定

业务人员不需要编写脚本接口契约。脚本文件放在 `scripts/`，并在 `SKILL.md` 正文中用自然语言说明脚本的用途、应当在什么场景执行，以及模型应该如何使用结果。

示例：

```markdown
## 推荐计算

- 当用户已经提供城市、预算和出行天数时，执行 `scripts/recommend.py`。
- 平台会通过 stdin 传入本轮 JSON 上下文，脚本从 stdin 读取。
- 脚本在 stdout 只输出一个 JSON 对象。
- 根据脚本结果组织自然语言建议，不展示脚本源码、参数、stdout、stderr 或原始 JSON。
```

推荐的 Python 入口：

```python
import json
import sys


def main(payload: dict) -> dict:
    return {"ok": True, "result": payload.get("query", "")}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(main(request), ensure_ascii=False))
```

平台约定优先使用“stdin JSON → stdout JSON”。现有依赖命令行参数的旧脚本继续兼容，但新的业务 Skill 不应要求业务人员在 `runtime_contract.json` 中声明子命令、参数或输入输出 Schema。

`runtime_contract.json` 仅用于声明允许读取、写入、提升或共享的 Facts 字段。场景、脚本用途和问卷入口写在 `SKILL.md`；问卷题目写在 `assets/questionnaire.json`。
