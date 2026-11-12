# AKEData 数据存储位置、调用方式与数据结构总结

**数据源**：https://data.akedata.wiki/  
**主站点**：https://www.akedata.wiki/  
**最新版本**：1.4.4@8764515-7（2026-07-23）

---

## 1. 数据调用方式

数据不是通过固定静态 URL 直接提供，而是由客户端动态构建 URL。

### 核心调用流程（来自客户端 JS：ake-data-source.js）

1. **Manifest**：先加载 `manifest.json` 获取版本清单
   - URL 示例：`https://data.akedata.wiki/manifest.json`

2. **构建请求 URL**：
   - 默认 `baseUrl = https://data.akedata.wiki/`
   - 根据资源类型动态拼接路径

3. **版本控制**：
   - 使用 `sharedRevision`（如 `2026-07-23T10:29:27.187600+00:00`）
   - 加上查询参数 `?v=...` 或 `?t=...` 强制刷新

4. **设置修改**：
   - 在 https://www.akedata.wiki/ 的 **⚙️ 设置** → **Game data** 中修改：
     - Request origin（数据源域名）
     - Data version（版本选择）

---

## 2. 数据目录结构

### A. Table Data（关卡资料、角色、武器、敌人等）  
存放于：`public/{gameVersion}/{hotfixVersion}/TableCfg/`

典型路径：
```
https://data.akedata.wiki/public/1.4.4/8764515-7/TableCfg/
```

包含的主要文件：
- `characters.json` —— 角色资料（关卡变体等）
- `weapons.json` —— 武器资料
- `enemies.json` —— 敌人资料
- `levels.json` —— 关卡资料（你最需要的副本文本内容）
- 其他 table 文件（如 skills、items 等）

**直接调用示例**：
```http
GET https://data.akedata.wiki/public/1.4.4/8764515-7/TableCfg/levels.json
GET https://data.akedata.wiki/public/1.4.4/8764515-7/TableCfg/characters.json
```

### B. Shared Data（共享静态数据）  
存放于：`public/{gameVersion}/{hotfixVersion}/Json/`

路径示例：
```
https://data.akedata.wiki/public/1.4.4/8764515-7/Json/...
```

包含各种公共 JSON 数据（如配置、文本、技能等）。

### C. 图片与资源文件  
存放于：`public/images/`

路径示例：
```
https://data.akedata.wiki/public/images/.../xxx.png
```

---

## 3. 参考项目

**https://github.com/NagiYume/AKEData/**  
该项目很可能为官方或社区提供的客户端/数据工具，可用于更好地理解数据结构和调用方式。

---

## 4. 使用建议

1. 想获取**关卡资料（副本文本内容）**，优先调用：
   ```json
   https://data.akedata.wiki/public/1.4.4/8764515-7/TableCfg/levels.json
   ```

2. 如果上面路径 404，可在浏览器打开 https://www.akedata.wiki/ → 按 F12 → Network 标签，搜索 `TableCfg` 或 `data.json`，即可看到实时加载的 JSON 文件。

3. 推荐使用 **数据源设置为 https://data.akedata.wiki/** + **版本选择最新** 来获取最新数据。

---

**数据结构说明**（简要）：
- 关卡资料（levels.json）包含公开静态信息，不含玩家挑战记录。
- 角色、武器、敌人资料包含关卡变体配置。
- 数据版本化严格，按游戏版本 + hotfix 号分开。

---

**来源**：基于 https://www.akedata.wiki/ 客户端 JS 代码 + 数据源分析 + manifest.json。