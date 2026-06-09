# 家教反馈工具 v2.0

一个面向家教 / 一对一辅导老师的 **AI 课后反馈生成助手**。上传作业、试卷、错题等文件，自动生成发给家长的课后反馈文案。基于 Flask，支持多家 LLM 供应商。

## 功能特性

- 📄 **文件解析**：支持 PDF、Word（docx）、图片、纯文本上传，自动提取内容
- 🤖 **多 API 供应商**：兼容 OpenAI 格式接口与 Anthropic（Claude）接口，API Key 在前端配置，按需调用
- ✍️ **个性化风格**：可为每位老师保存语气 / 风格偏好（`style_instruction`），让反馈更像「本人在说话」
- 👥 **多用户 / 多学生**：按设备区分用户，分别管理学生与历史记录
- 🗂️ **历史记录**：保存每次生成的反馈，便于回溯

## 技术栈

- 后端：Flask 3
- 文件解析：PyMuPDF（PDF）、python-docx（Word）
- 前端：原生 HTML / CSS / JS（`templates/index.html` + `static/`）

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动
python app.py

# 3. 浏览器打开
# http://127.0.0.1:8686
```

API Key（OpenAI 兼容或 Anthropic）在页面内配置，无需写入代码。

## 目录结构

```
├── app.py              # Flask 后端主程序
├── requirements.txt    # 依赖
├── templates/
│   └── index.html      # 前端页面
├── static/             # 样式、字体、封面
└── data/               # 用户与历史数据（已 gitignore，不上传）
```

> ⚠️ `data/` 目录包含真实的老师与学生反馈数据，已通过 `.gitignore` 排除，不会进入版本库。

## License

MIT
