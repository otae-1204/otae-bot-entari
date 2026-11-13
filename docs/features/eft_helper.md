# 逃离塔科夫弹药查询（plugins/eft_helper）

已废弃的空包：`__init__.py` 为空，不注册任何命令或监听，加载后什么也不做。目录里留有弹药数据（api.tarkov.dev）的数据层与绘图代码，但没有入口引用它们。

`tests/test_mcsm_commands.py` 校验它保持为空入口，`/状态` 公开版也不列它。

## 命令

无。

## 配置

`plugins.eft_helper.use_proxy`（请求 api.tarkov.dev 时是否走 `HTTP_PROXY`，默认 false）。仍在 `entari.example.yml` 里，但当前不会被用到。

## 数据

无。数据层若被调用会建 `assets/sql/eft_helper/ammo.db`，目前不会发生。
