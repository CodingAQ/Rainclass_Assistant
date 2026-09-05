# 长江雨课堂自动助手

这是一个用于长江雨课堂的自动化工具，旨在帮助学生自动完成随堂测试和签到。该工具通过模拟浏览器操作，结合单个 AI 模型或多 AI 并行投票自动识别题目并给出答案，同时支持微信通知提醒。

## 功能特性

- **自动答题**: 自动识别并提交客观题；主观题只生成 AI 参考答案，不自动提交。
- **AI模型支持**: 支持豆包 AI、Gemini AI、自定义 OpenAI 兼容接口，以及由多个兼容接口并行投票的多 AI 模式。
- **静默运行**: 支持无头模式（headless mode），可在后台静默运行，不显示浏览器界面。
- **Cookies管理**: 自动获取和保存登录Cookies，并提供Cookies有效期提醒。
- **微信通知**: 通过xxtui平台发送微信通知，及时提醒用户签到、答题情况及Cookies过期预警。
- **Playwright 浏览器**: 使用独立 Chromium。

## 安装与运行

### 1. 克隆仓库

```bash
git clone https://github.com/CodingAQ/Rainclass_Assistant.git
cd Rainclass_Assistant
```

### 2. 安装依赖

确保系统已安装 Python 3.10 或更高版本，然后安装依赖和 Playwright Chromium：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. 配置 AI API Key 和微信提醒

在首次运行程序后，会在项目根目录生成 `config.json` 文件。您也可以通过图形界面在“设置”页面进行配置：

- **AI模型选择**: 选择`豆包AI`、`Gemini AI`、`自定义`或`多AI作答`。
- **豆包AI API Key / Gemini AI API Key**: 填写您选择的AI模型的API Key。
- **多AI作答**: 默认读取项目根目录的 `model_visible.ini`，每个 section 需配置 `base_url`、`key` 和 `model`。所有模型同时请求，默认最多等待 20 秒；设置页可修改配置文件路径和最大等待时间。选择多 AI 后点击“测试”会同时测试文件中的全部模型。
- **微信提醒API Key**: 填写您的xxtui API Key，用于接收微信通知。

### 4. 获取登录 Cookies

在“设置”页面点击“获取登录Cookies”按钮。程序将启动一个浏览器窗口，请在该窗口中完成长江雨课堂的登录。登录成功后，Cookies将自动保存，并用于后续的自动化操作。

### 5. 运行程序

```bash
python main.py
```

程序将启动一个图形界面。在“主页”点击“启动自动答题”即可开始自动化任务。

### 6. 运行回归测试

```bash
python -m unittest discover -s tests -v
```

## 使用说明

1. **首次运行**: 
   - 运行 `python main.py` 启动程序。
   - 切换到“设置”页面。
   - 配置您的AI API Key和微信提醒API Key。
   - 点击“获取登录Cookies”并完成登录。
   - 保存设置。
2. **启动/停止**: 在“主页”点击“启动自动答题”或“停止自动答题”按钮来控制程序的运行。
3. **监控日志**: 在“主页”的日志区域可以实时查看程序的运行状态和操作记录。
4. **Cookies有效期**: 程序会显示Cookies的有效期，并在即将过期时通过微信提醒您。请及时更新Cookies以确保程序正常运行。


## 贡献

欢迎提交 Issue 或 Pull Request 来改进此项目。

## 许可证

本项目采用 MIT 许可证。

## 致谢

本项目参考 https://github.com/501Ranger/Rainclass_Assistant