# ContentAI

该目录为完整可运行代码项目（含后端、前端、运行配置、虚拟环境）。

文档入口：../Docs
UI 资料入口：../UI
SQLite 数据库：../Sql

## 配置说明（DB 化）

- `configs` 目录不再参与运行时读取（账号配置与系统配置已全部走数据库）。
- `configs/accounts/*.json` 与 `configs/system_config.json` 仅作为历史遗留文件，不再用于启动回退迁移。
- 应用启动由数据库初始化决定；首次启动且数据库为空时，会自动写入内置默认账号。
- SQLite 与运行制品统一写入上级目录 `../Sql`，该目录不上传 Git。

