from utils.database import SQLiteHelper
from configs.path_config import SQL_PATH, IMAGE_PATH
from .entity import Ammo
from configs.config import Plugin_Config, SYSTEM_PROXY
import httpx

sql = SQLiteHelper("ammo.db", SQL_PATH+"eft_helper/")

config = Plugin_Config("eft_helper")

# 口径映射
caliber_mapping = {
    'Caliber556x45NATO': "5.56x45mm",
    'Caliber12g': "12/70",
    'Caliber762x54R': "7.62x54R",
    'Caliber762x39': "7.62x39mm",
    'Caliber40mmRU': "40x46mm",
    'Caliber9x19PARA': "9x19mm",
    'Caliber545x39': "5.45x39mm",
    'Caliber762x25TT': "7.62x25mm",
    'Caliber9x18PM': "9x18mmPM",
    'Caliber9x39': "9x39mm",
    'Caliber762x51': "7.62x51mm",
    'Caliber366TKM': ".366",
    'Caliber9x21': "9x21mm",
    'Caliber20g': "20/70",
    'Caliber46x30': "4.6x30mm",
    'Caliber127x55': "12.7x55mm",
    'Caliber57x28': "5.7x28mm",
    'Caliber1143x23ACP': ".45ACP",
    'Caliber23x75': "23x75mm",
    'Caliber40x46': "40x46mm",
    'Caliber762x35': ".300 AAC Blackout",
    'Caliber86x70': ".338 Lapua Magnum",
    'Caliber9x33R': ".357 Magnum",
    'Caliber26x75': "26x75mm",
    'Caliber68x51': "6.8x51mm"
}


class DataSource:

    def __init__(self):
        # 判断是否存在数据库及表是否存在
        if not sql.connection:
            sql.connect()
        query = "CREATE TABLE IF NOT EXISTS ammo( \
            id INTEGER PRIMARY KEY AUTOINCREMENT, \
            name TEXT NOT NULL, \
            caliber TEXT NOT NULL, \
            weight REAL NOT NULL, \
            stackMaxSize INTEGER NOT NULL, \
            tracer BOOLEAN NOT NULL, \
            tracerColor TEXT NOT NULL, \
            damage TEXT NOT NULL, \
            armorDamage REAL NOT NULL, \
            fragmentationChance REAL NOT NULL, \
            ricochetChance REAL NOT NULL, \
            penetrationPower INTEGER NOT NULL, \
            accuracyModifier REAL NOT NULL, \
            recoilModifier REAL NOT NULL, \
            lightBleedModifier REAL NOT NULL, \
            heavyBleedModifier REAL NOT NULL, \
            img TEXT NOT NULL, \
            marketSale BOOLEAN NOT NULL, \
            apiID TEXT NOT NULL, \
            projectileCount INTEGER NOT NULL, \
            initialSpeed REAL NOT NULL, \
            staminaBurnPerDamage REAL NOT NULL)"
        sql.execute(query)

    @staticmethod
    def get_ammo_data_by_id(ammo_id: int) -> list:
        """通过ID获取弹药数据"""
        query = "SELECT * FROM ammo WHERE id = ?"
        return sql.fetchall(query, (ammo_id,))

    @staticmethod
    def get_ammo_data_by_name(ammo_name: str) -> list:
        """通过名称获取弹药数据"""
        query = "SELECT * FROM ammo WHERE name = ?"
        return sql.fetchall(query, (ammo_name,))

    @staticmethod
    def get_ammo_data_by_caliber(ammo_caliber: str) -> list:
        """通过口径获取弹药数据"""
        query = "SELECT * FROM ammo WHERE caliber = ?"
        return sql.fetchall(query, (ammo_caliber,))

    @staticmethod
    def get_ammo_data_by_api_id(api_id: str) -> list:
        """通过API ID获取弹药数据"""
        query = "SELECT * FROM ammo WHERE apiID = ?"
        return sql.fetchall(query, (api_id,))

    @staticmethod
    def get_ammo_data_by_rule(rule: str, params: tuple = ()) -> list:
        """通过规则获取弹药数据（参数化查询）"""
        query = "SELECT * FROM ammo WHERE " + rule
        return sql.fetchall(query) if not params else sql.fetchall(query, params)

    @staticmethod
    def add_ammo(ammo: Ammo) -> bool:
        """添加弹药数据"""
        query = "INSERT INTO ammo VALUES( \
            :name, :caliber, :weight, :stackMaxSize, :tracer, :tracerColor, :damage, :armorDamage, \
            :fragmentationChance, :ricochetChance, :penetrationPower, :accuracyModifier, :recoilModifier, \
            :lightBleedModifier, :heavyBleedModifier, :img, :marketSale, :apiID, :projectileCount, :initialSpeed, \
            :staminaBurnPerDamage)"
        sql.execute(query, ammo.__dict__)
        return True

    @staticmethod
    def update_ammo(ammo: Ammo) -> bool:
        """更新弹药数据"""
        query = "UPDATE ammo SET \
            name = :name, caliber = :caliber, weight = :weight, stackMaxSize = :stackMaxSize, \
            tracer = :tracer, tracerColor = :tracerColor, damage = :damage, armorDamage = :armorDamage, \
            fragmentationChance = :fragmentationChance, ricochetChance = :ricochetChance, penetrationPower = :penetrationPower, \
            accuracyModifier = :accuracyModifier, recoilModifier = :recoilModifier, lightBleedModifier = :lightBleedModifier, \
            heavyBleedModifier = :heavyBleedModifier, img = :img, marketSale = :marketSale, apiID = :apiID, \
            projectileCount = :projectileCount, initialSpeed = :initialSpeed, staminaBurnPerDamage = :staminaBurnPerDamage \
            WHERE id = :id"
        sql.execute(query, ammo.__dict__)
        return True

    @staticmethod
    def delete_ammo(ammo_id: int) -> bool:
        """删除弹药数据"""
        query = "DELETE FROM ammo WHERE id = ?"
        sql.execute(query, (ammo_id,))
        return True

    @staticmethod
    async def update_ammo_from_api():
        query_cn = """
        {
            ammo(lang:zh){
                item{
                    id #id
                    name #名称
                    iconLink #图标
                    avg24hPrice #24平均价格
                }
          	    weight #重量
                caliber #口径
          	    stackMaxSize #最大堆叠数量
          	    tracer #是否曳光
        		tracerColor #曳光颜色
              	damage #肉伤
          	    armorDamage #损甲百分比
             	fragmentationChance #碎弹率
          	    ricochetChance #跳弹率
              	penetrationPower #穿透值
          	    accuracyModifier #精度修正
              	recoilModifier #后座修正
          	    lightBleedModifier #小出血修正
              	heavyBleedModifier #大出血修正
                projectileCount #弹丸数量
                initialSpeed #初速
                staminaBurnPerDamage #消耗体力
          }
        }
        """
        headers = {"Content-Type": "application/json"}
        try:
            proxies_dict = SYSTEM_PROXY.get("http") if config.plugin_content.get("use_proxy") else None

            async with httpx.AsyncClient(proxies=proxies_dict) as client:
                response = await client.post('https://api.tarkov.dev/graphql', json={'query': query_cn}, headers=headers, timeout=30)
                if response.status_code != 200:
                    return False
                data = response.json()["data"]["ammo"]
                for ammo in data:
                    ammo_obj = Ammo(
                        name=ammo["item"]["name"],
                        caliber=caliber_mapping.get(ammo["caliber"], ammo["caliber"]),
                        weight=ammo["weight"],
                        stackMaxSize=ammo["stackMaxSize"],
                        tracer=ammo["tracer"],
                        tracerColor=ammo["tracerColor"],
                        damage=ammo["damage"],
                        armorDamage=ammo["armorDamage"],
                        fragmentationChance=ammo["fragmentationChance"],
                        ricochetChance=ammo["ricochetChance"],
                        penetrationPower=ammo["penetrationPower"],
                        accuracyModifier=ammo["accuracyModifier"],
                        recoilModifier=ammo["recoilModifier"],
                        lightBleedModifier=ammo["lightBleedModifier"],
                        heavyBleedModifier=ammo["heavyBleedModifier"],
                        img=f"{ammo['item']['name']}.png",
                        marketSale=isinstance(ammo["item"]["avg24hPrice"], int),
                        apiID=ammo["item"]["id"],
                        projectileCount=ammo["projectileCount"],
                        initialSpeed=ammo["initialSpeed"],
                        staminaBurnPerDamage=ammo["staminaBurnPerDamage"]
                    )
                    # 下载图片并保存成png
                    img_response = await client.get(ammo["item"]["iconLink"])
                    with open(f"{IMAGE_PATH}/{ammo_obj.name}.png", "wb") as f:
                        f.write(img_response.content)
                    # 添加数据
                    DataSource.add_ammo(ammo_obj)

            return True

        except Exception as e:
            print(e)
            return False

