# CaptionCheck

用于审查/修正 `data/<sport>/<event>/long_caption.json` 的小工具。

## 安装

使用 `uv` 管理依赖。建议用 Python 3.12.x（仓库已提供 `.python-version`）。

```bash
uv venv --python python3.12
uv sync
```

## 配置

仓库内已提供默认配置 `captioncheck_config.json`，可按需修改（例如外部编辑器命令）。

## 运行

```bash
uv run captioncheck
```

首次启动会对数据集做一次增量预处理（每个 `sport/event` 目录生成 `preprocess_status.json`，并把 `spans[].start_frame/end_frame` 变为从 0 开始，同时在 `long_caption.json` 顶层加入 `reviewed` 字段）。
