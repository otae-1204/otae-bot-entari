# 参考资料

外部接口记录、调研与转载资料。内容以各自的记录时间为准，不是本项目的规则；
项目约定见 [架构规范](../development/architecture_conventions.md)。

## Entari 插件编写指南（上游通用资料）

[`entari-plugin-writing.md`](entari-plugin-writing.md) 是用户于 2026-10-07 提供的
Entari 插件编写参考，原文件名为 `SKILL (2).md`。此处保留完整原文，未改写示例。
复制时已核对原件与副本一致，原件的 SHA-256 为
`0ed20547065b9bc9d25b4f6e22815b01770e9a9fc1543a5aaa70023f45fa2312`。
Git 检出时可能转换换行符，跨平台校验时需留意这一差异。

它是面向独立发行插件的通用资料，不是本项目的开发规范。其中的依赖示例为
`arclet-entari>=0.18.0`；本项目在 `pyproject.toml` 固定 **0.19.4**，涉及 API 时以已安装版本为准。
本项目怎样写插件见 [新增插件](../development/adding_a_plugin.md)；功能仍放在 `plugins/<目录名>/`，
连接配置在 `entari.yml` 的 `basic.network`（或 `plugins.server`），见
[连接方式](../operations/connections.md)。

## 其他资料

| 文件 | 内容 |
|---|---|
| [ai_radar_interface.md](ai_radar_interface.md) | AI 雷达上游 API 实测（2026-09-13） |
| [ai_radar_data_dictionary.md](ai_radar_data_dictionary.md) | AI 雷达上游字段释义 |
| [ai_radar_frontend_api.md](ai_radar_frontend_api.md) | `plugins/radar` 的数据接口说明（2026-09 编写，以代码为准） |
| [akedata_data_access_guide.md](akedata_data_access_guide.md) | AKEData 取数指南 |
| [ake-research/](ake-research/README.md) | AKEData 站点研究文章的转载 |
| [endfield_product_facts.md](endfield_product_facts.md) | 终末地挑战页的事实记录 |
| [endfield_warfarin_wiki.md](endfield_warfarin_wiki.md) | Warfarin Wiki 数据源记录（2026-06） |
| [fz_wiki_open_api_summary.md](fz_wiki_open_api_summary.md) | fz.wiki 开放 API 调研（2026-07） |
| [endfield_ui_asset_extraction.md](endfield_ui_asset_extraction.md) | 本机游戏 UI 资源提取流程 |
| [skland_endfield_personal_api.md](skland_endfield_personal_api.md) | 森空岛终末地个人接口逆向 |
| [skland_endfield_public_query.md](skland_endfield_public_query.md) | 森空岛公开数据独立查询脚本 |
| [skland_endfield_ui_data_inventory.md](skland_endfield_ui_data_inventory.md) | 森空岛接口 UI 字段清单 |
| [skland_medal_id_mapping.md](skland_medal_id_mapping.md) | 森空岛奖章 id 与 AKEData 的对应 |
