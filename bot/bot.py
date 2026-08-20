import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 初始化 NoneBot(读取同目录 .env / .env.prod 配置)
nonebot.init()

# 注册 OneBot v11 适配器(NapCat 走的就是这个协议)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载 src/plugins 目录下的所有插件 —— 你的插件就放这里
nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()
