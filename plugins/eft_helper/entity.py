# Description: 存储实体类的文件
from dataclasses import dataclass

@dataclass()
class Ammo():
    id: int  # id
    name: str  # 名称
    caliber: str  # 口径
    weight: float  # 重量
    stackMaxSize: int  # 堆叠数量
    tracer: bool  # 是否有弹迹
    tracerColor: str  # 弹迹颜色
    damage: str  # 肉伤
    armorDamage: float  # 护甲伤害
    fragmentationChance: float  # 碎弹几率
    ricochetChance: float  # 跳弹几率
    penetrationPower: int  # 穿甲力
    accuracyModifier: float  # 精度修正
    recoilModifier: float  # 后坐力修正
    lightBleedModifier: float  # 小出血修正
    heavyBleedModifier: float  # 大出血修正
    img: str  # 图片名
    marketSale: bool  # 是否禁售
    apiID: str  # API ID
    projectileCount: int  # 弹丸数量
    initialSpeed: float  # 初速
    staminaBurnPerDamage: float  # 消耗体力

# 购买来源
@dataclass()
class ItemPrice:
    """
    存储购买/售出信息的类
    """
    price: int  # 价格
    currency: str  # 货币
    priceRUB: int  # 价格（卢布）
    source: str  # 来源
    requirements: list[dict]  # 需求材料 dict

# 合成来源
@dataclass()
class Craft:
    """
    存储合成信息的类
    """
    name: str  # 名称
    level: int  # 等级
    duration: int  # 时间
    requirements: list[dict]  # 材料 dict(name, count)

# 子弹详细信息类
@dataclass()
class AmmoMoreInfo:
    """
    存储子弹额外信息的类
    """
    basePrice: int  # 基础价格
    avg24hPrice: int  # 24小时平均价格
    fleaMarketPrice: int  # 跳蚤市场最近价格
    buyFor: list[ItemPrice]  # 购买来源
    craftsFor: list[Craft]  # 合成来源