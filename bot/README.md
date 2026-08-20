# QQ Bot (NoneBot2) —— 插件写在这里

这是机器人框架项目,通过 OneBot v11 连接 NapCat(QQ 网关)。
**你的插件就写在 `src/plugins/` 下,一个插件一个文件夹。**

## 目录结构

```
bot/
├── bot.py                 # 启动入口
├── .env                   # 运行配置(端口/驱动/超级用户)
├── requirements.txt       # Python 顶层依赖
├── constraints-runtime.txt # 已验证运行环境的精确版本约束
├── requirements-dev.txt   # pytest / Ruff / Pyright 精确版本
└── src/
    └── plugins/           # ← 插件目录,新建文件夹写你的插件
        └── echo/          # 示例插件(关键词回复 + /echo 命令)
            └── __init__.py
```

## 怎么新增一个插件

1. 在 `src/plugins/` 下新建一个文件夹,如 `weather/`
2. 里面建 `__init__.py`,写你的逻辑(参考 `echo/__init__.py`)
3. 重启 `bot.py` 即可自动加载

## 运行

```bash
cd bot
python bot.py
```

启动后 NoneBot 在 `0.0.0.0:8080` 开一个反向 WS 服务端,
NapCat(容器)通过 `ws://host.docker.internal:8080/onebot/v11/ws` 连进来。

## 开发与验证

运行环境以 Apple Silicon + Python 3.13 为基线。安装时应用已验证约束，避免一次普通
部署意外升级传递依赖：

```bash
cd bot
.venv/bin/pip install -r requirements.txt -c constraints-runtime.txt
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
```

升级依赖应单独提交：先在干净虚拟环境验证，再更新 `constraints-runtime.txt`，执行
`pip check` 和上述全部检查。首次安装 Playwright 后仍需执行
`.venv/bin/playwright install chromium`。

持续扩展 `/cs2` 前请阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 与
[FEATURE_CHECKLIST.md](FEATURE_CHECKLIST.md)。

## 连接关系

```
手机QQ  ──扫码──▶  NapCat(Docker,QQ↔OneBot 网关)
                        │  反向 WS(OneBot v11)
                        ▼
                 NoneBot2(本项目)──加载──▶ src/plugins/*
```
