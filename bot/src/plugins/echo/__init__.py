"""示例插件:演示插件目录结构与最基本的收发消息。

复制这个 echo 文件夹改个名字,就是你的新插件。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="复读示例",
    description="示例插件:演示插件目录结构与最基本的收发消息",
    usage="/echo <内容>  原样复读",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 1) 关键词触发:消息里含“你好”或“在吗”时回复“在的~”
#    已按需求关闭——如需恢复,取消下面这段的注释即可。
# hello = on_keyword({"你好", "在吗"}, priority=10, block=True)
#
#
# @hello.handle()
# async def _():
#     await hello.finish("在的~")


# 2) 命令触发:发 “/echo 内容” 时把内容原样复读
echo = on_command("echo", priority=10, block=True)


@echo.handle()
async def handle_echo(args: Message = CommandArg()) -> None:  # noqa: B008
    text = args.extract_plain_text().strip()
    if text:
        await echo.finish(text)
    await echo.finish("用法:/echo 要复读的内容")
