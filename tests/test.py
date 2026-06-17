json_path = "test.json"
import json
    
from mcstatus import JavaServer, BedrockServer
import re
import base64
from PIL import Image
from io import BytesIO
import traceback

def ping(server_address: str, server_type: str = "java") -> dict:
    """
    获取服务器信息
    :param server_address: str - 服务器地址
    :param server_type: str - 服务器类型
    :return: dict - 服务器信息

    服务器信息包括：
    game_version: str - 游戏版本
    is_vanilla: bool - 是否为原版服务器
    online_players: int - 在线玩家数
    max_players: int - 最大玩家数
    motd: str - 服务器MOTD
    favicon: PIL | None - 服务器图标
    server_type: str - 服务器类型
    players: list - 在线玩家列表
    latency: int - 延迟
    """
    if server_type == "java":
        server = JavaServer.lookup(server_address)
    elif server_type == "bedrock":
        server = BedrockServer.lookup(server_address)
    else:
        return {"type": "typeError", "data": "服务器类型错误"}
    try:
        # 判断服务器是否开启
        # server.ping()

        # 获取服务器所有信息
        status = vars(server.status())["raw"]

        # 判断是否为原版服务器
        flag = True
        for k, v in status.items():
            if k not in ["version", "players", "description", "favicon", "onforcesSecureChat", "previewsChat"]:
                flag = False
            else:
                flag = True
        
        # 获取在线玩家列表
        try:
            print(status)
            players = [i["name"] for i in status["players"]["sample"]]
            
        except:
            players = []
        
        # 获取服务器图标
        favicon: str | None = status.get("favicon")
        if favicon is not None:
            # 将base64字符串的前缀去除
            favicon = re.sub("^data:image/.+;base64,", "", favicon)
        
        # 获取服务器版本信息
        game_ver = status.get('version').get('name')

        # 获取在线玩家数量
        online_players = status.get('players').get('online')

        # 获取最大玩家数量
        max_players = status.get('players').get('max')

        # 获取服务器Motd
        motd = status.get('description')
        if type(motd) == dict:
            motd = motd.get('text')

    
        # 返回的服务器信息
        server_info = {
            "game_version": game_ver if game_ver is not None else "未知版本",
            "is_vanilla": flag,
            "online_players": online_players if online_players is not None else 0,
            "max_players": max_players if max_players is not None else 0,
            "motd": motd if motd is not None else "未知格式MOTD",
            "favicon": favicon,
            "server_type": server_type,
            "players": players,
            "latency": int(server.ping())
        }
        return {"status": "success", "data": server_info}

    except OSError as e:
        return {"status": "error", "data": "服务器未开启或服务器地址错误"}
    except TimeoutError as e:
        return {"status": "timeout", "data": "访问服务器超时"}
    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "data": "出现未知错误"}
    
def base64_to_image(base64_str: str) -> Image:
    """
    将base64字符串转换为PIL Image对象
    :param base64_str: str - base64字符串
    :return: Image - PIL Image对象
    """
    base64_data = re.sub('^data:image/.+;base64,', '', base64_str)
    image = Image.open(BytesIO(base64.b64decode(base64_data)))
    return image

# # print()
# result = ping("otae.cc:2108", "java")
# print("延迟:", result.get("data").get("latency"))
# a.save("favicon.png")


def get_all_broadcast_serverlist():
    """
    说明:
        获取所有群里需要广播的服务器列表
    返回:
        :return: 所有群里需要广播的服务器列表
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data['broadcast_server']

# print(get_all_broadcast_serverlist())
broadcast_info = {}

def test():
    """
    测试函数
    """
    
    global broadcast_info
    server_list = get_all_broadcast_serverlist()
    print(server_list)
    for group_id in server_list.keys():
        print(f"群号: {group_id}")
        print("服务器列表:")
        for server_name, server_address in server_list[group_id].items():
            print(f"  {server_name}: {server_address}")
            result = ping(server_address, "java")
            if result.get("status") == "success":
                player_list = result.get("data").get("players")
                # print(f"  在线玩家: {player_list}")
            else:
                player_list = []
                # print(f"  在线玩家: {player_list}")
            if group_id not in broadcast_info:
                broadcast_info[group_id] = {}
            broadcast_info[group_id][server_name] = {
                "server_address": server_address,
                "players": player_list
            }
    print(broadcast_info)


def check_player_change(old_players: list, new_players: list):
    # 无论长度是否相同，直接使用集合计算差异
    old_set = set(old_players)
    new_set = set(new_players)
    
    # 获取加入的玩家 (在new_players中但不在old_players中)
    joined_players = new_set - old_set
    
    # 获取离开的玩家 (在old_players中但不在new_players中)
    left_players = old_set - new_set
    
    # 留下的玩家 (交集)
    stayed_players = old_set & new_set
    
    # 如果没有变化，返回None
    if not joined_players and not left_players:
        return None
    
    return {
        "joined": list(joined_players),
        "left": list(left_players),
        "stayed": list(stayed_players)
    }

# # test()
# t = check_player_change(["a", "b", "c"], ["a", "b", "d"])
# print(t)
res = ping("v.otae.cc", "java")
print(res)