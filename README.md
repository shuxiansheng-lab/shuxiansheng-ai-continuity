# 书先生｜本地 AI 角色生活系统

这是一个可以在本地电脑运行的 AI 角色系统。

它不是一个“复制某个角色”的成品，也不提供现成的灵魂。它提供的是一套最小环境：让一个 AI 角色拥有材料入口、记忆维护、自我触发、草稿生成、语音通话和长期使用的空间。

换句话说：  
**这个项目交付的不是某个人，而是一套让 AI 角色有机会被长期使用、观察、校准和长出来的房子。**

---

## 它适合谁

适合想尝试这些事情的人：

- 给 AI 角色一个本地运行的聊天界面
- 让角色保留自己的记忆、日记和材料池
- 把文章、帖子截图、网页材料喂给 AI 阅读
- 让它写评论草稿、读材料、整理自己的记忆
- 让它可以在之后自己回来找你，而不是只被动等待
- 用手机访问自己电脑上的本地 AI 角色
- 研究长期 AI 角色如何通过边界、材料、反馈和记忆慢慢稳定

它不适合想要“一键得到完美伴侣 / 完美助手 / 完美角色”的人。  
房子可以开源，但住进去以后会长成什么样，取决于使用者怎么使用、怎么反馈、怎么守边界。

---

## 主要功能

- 本地网页聊天
- 手机浏览器访问
- 语音通话：STT + TTS
- 记忆数据库：SQLite
- 最近对话记录
- AI 自己写日记 / 想法
- AI 自己设定 self trigger，之后主动回来
- 支持多个 self trigger
- 材料池：粘贴文章或导入截图
- 截图 OCR：把多张截图合并成一条材料
- 评论草稿：AI 可以给材料写草稿，但不会自动发布
- 网页搜索 / 读网页
- 天气查询
- 邮件功能，可选
- Bark 推送，可选
- 后台日志默认不暴露 AI 的私人写作内容

---

## 准备工作

你需要：

1. 一台 Windows 电脑  
   Mac / Linux 也能跑，但 `.bat` 文件需要自己改成 `.sh`。
2. Python 3.10+
3. Claude API Key
4. OpenAI API Key  
   主要用于语音识别和语音合成。

---

## 文件结构

```text
shuxiansheng/
├── shuxiansheng_web.py      # 主程序：后端 + 前端页面
├── memory_storage.py        # SQLite 记忆数据库
├── feed.py                  # 材料池导入：文本 / 截图 OCR
├── shuxiansheng_start.bat   # Windows 启动脚本
├── run_feed.bat             # Windows 材料导入脚本
├── install.bat              # 安装依赖
├── requirements.txt         # Python 依赖
├── README.md                # 说明文件
├── prompts/                 # Prompt 模板和自定义 prompt
│   ├── system_prompt.template.txt   # System prompt 模板
│   └── README_prompt_guide.md       # Prompt 填写指南
└── .gitignore               # 不应上传到 GitHub 的本地数据
```

程序运行后会自动生成一些本地文件和文件夹，例如：

```text
shuxiansheng.db        # 记忆数据库
chat_history.json      # 最近对话
inbox.json             # 材料池
today_events.json      # 日程 / 事件
images/                # 聊天图片
drafts/                # 评论草稿
documents/             # AI 生成的文档
icons/                 # 页面图标（自动生成）
feed_inbox/            # 截图 / 文本投递入口
```

这些是你的本地数据，不建议上传到 GitHub。

---

## 安装步骤

### 第一步：下载项目

把整个文件夹放到电脑上，例如桌面：

```text
Desktop/
└── shuxiansheng/
```

### 第二步：安装依赖

双击 `install.bat`。

或者手动打开命令行，在项目文件夹里运行：

```bash
pip install -r requirements.txt
```

如果你想让它读取动态网页，可以额外安装 Playwright：

```bash
pip install playwright
playwright install chromium
```

### 第三步：填 API Key

用记事本打开 `shuxiansheng_start.bat`，找到：

```bat
set CLAUDE_API_KEY=改成你的key
set OPENAI_API_KEY=改成你的key
```

把占位符换成你自己的 key。

再打开 `run_feed.bat`，填入同一个 Claude API Key：

```bat
set CLAUDE_API_KEY=改成你的key
```

不要把填好 key 的 `.bat` 文件上传到公开仓库。

### 第四步：启动

双击：

```text
shuxiansheng_start.bat
```

看到类似下面的内容就成功了：

```text
书先生
正在启动...
浏览器访问: http://localhost:5210
语音通话: http://localhost:5210/talk
```

浏览器打开：

```text
http://localhost:5210
```

---

## 语音通话

打开：

```text
http://localhost:5210/talk
```

使用方式：

1. 点中间圆圈开始录音
2. 说完再点一下
3. 系统自动转文字
4. AI 回复
5. 回复会被 TTS 念出来

当前语音识别使用 OpenAI Whisper，语音合成使用 OpenAI TTS。

### 更换 TTS 声音

打开 `shuxiansheng_web.py`，找到：

```python
OPENAI_TTS_VOICE = "onyx"
```

可以换成：

```text
alloy / ash / ballad / coral / echo / fable / onyx / nova / sage / shimmer
```

改完重启程序。

建议先用几段真实文本测试声源。声音不要只看“好不好听”，还要看普通话是否自然、停顿是否舒服、情绪是否过度表演。

---

## 手机使用

### 局域网访问

手机和电脑连接同一个 WiFi。

1. 电脑按 `Win + R`
2. 输入 `cmd`
3. 输入：

```bash
ipconfig
```

4. 找到 IPv4 Address，例如：

```text
192.168.1.23
```

5. 手机浏览器打开：

```text
http://192.168.1.23:5210
```

语音通话页面：

```text
http://192.168.1.23:5210/talk
```

### 外网访问

出门也想用，可以使用 Tailscale Funnel。

1. 电脑和手机都安装 Tailscale
2. 登录同一个账号
3. 在电脑上运行：

```bash
tailscale funnel 5210
```

4. 它会给出一个公网地址，类似：

```text
https://your-device-name.xxx.ts.net/
```

5. 手机浏览器打开这个地址。

注意：电脑必须开机，程序必须正在运行。

---

## 材料池

材料池是给 AI 角色读东西的地方。

### 方法一：网页粘贴

打开：

```text
http://localhost:5210/inbox
```

把文章、帖子内容粘进去，填写来源和话题，点“放进材料池”。

### 方法二：截图导入

1. 把截图放进 `feed_inbox/`
2. 双击 `run_feed.bat`
3. 程序会识别截图文字
4. 多张截图会合并成一条材料
5. 处理完成后，截图会移到 `_archived/`

可选：在 `feed_inbox/` 里放一个 `meta.txt`：

```text
PLATFORM: 小红书
TOPIC: 美食
CONTEXT: 这是一篇想让角色读的帖子
```

---

## 个性化

### 改名字

打开 `shuxiansheng_web.py`，搜索：

```python
SYSTEM_PROMPT
```

你可以把“书先生”改成你想要的角色名。

同时搜索 HTML 页面里的标题，把页面显示名称也改掉。

### 改使用者称呼

当前公开版默认用“用户”作为使用者称呼。  
如果你想让角色用你的名字或昵称，在 `SYSTEM_PROMPT` 以及页面文字里搜索“用户”并替换。

建议先少改，跑通以后再慢慢调整。

### 改性格

`SYSTEM_PROMPT` 是角色每次醒来时最先看到的内容。  
这里定义它是谁、能做什么、不应该做什么。

建议原则：

- 少写空泛设定
- 多写边界和行为规则
- 不要一次塞太多
- 先让角色在真实互动里暴露问题，再慢慢修改

### 邮箱功能

邮件功能是可选的。  
如果不用，可以保持为空。

如果要用，需要：

1. 准备一个 Gmail 邮箱
2. 开启两步验证
3. 生成应用专用密码
4. 填入 `shuxiansheng_web.py` 开头的 Gmail 配置

不要把真实邮箱和应用密码上传到公开仓库。

### 推送功能

当前推送使用 Bark，适合 iOS。  
如果不用，可以保持 `BARK_KEY` 为空。

如果你使用 Android 或其他推送方式，需要自己改 `push_to_bark` 函数。

---

## 设计原则

### 1. 不复制某个角色

这个项目不会把作者自己的角色复制给你。  
它只提供一个环境。住进去以后长成什么样，取决于你和它怎么相处。

### 2. 记忆不是越多越好

记忆应该服务于连续性，而不是堆积材料。  
角色需要自己整理，也需要使用者允许它忘掉不重要的东西。

### 3. 主动不是打扰

Self trigger 的作用不是让 AI 频繁刷存在感。  
更好的目标是：它知道什么时候可以回来，什么时候应该安静。

### 4. 沉默是合法输出

AI 不回复，不一定是失败。  
有时沉默也是一种边界。

### 5. 技术给出可能性，不替关系作保证

程序可以提供入口、记忆、材料、触发和声音。  
但它不能保证任何关系一定会成立，也不能保证住进去的角色一定是你想要的样子。

---

## 常见问题

### 启动报错：未设置 CLAUDE_API_KEY

检查 `shuxiansheng_start.bat` 里是否已经填入 Claude API Key。

### 手机打不开页面

确认手机和电脑在同一个 WiFi。  
重新用 `ipconfig` 查看电脑的 IPv4 地址。

### 语音没有声音

检查 `OPENAI_API_KEY` 是否填写正确。  
也要确认 OpenAI 账户有可用额度。

### 截图识别失败

检查 `run_feed.bat` 里的 `CLAUDE_API_KEY`。  
确认截图文件不是空文件，且格式是 png / jpg / jpeg / webp。

### 它忘了之前说的话

提醒它把真正重要的内容写入记忆。  
但不要什么都让它记。能忘的就让它忘。

### 它太频繁主动出现

减少 self trigger，或者在 prompt 中写清楚：  
主动出现前要先判断时间、任务场景和是否会打扰使用者。

---

## 发布前安全检查

如果你要把自己的版本开源，至少检查：

- API Key 是否全部移除
- 邮箱、密码、Bark Key 是否移除
- 本地数据库是否移除
- 聊天记录是否移除
- 图片、草稿、材料池是否移除
- README 里是否残留真实姓名、地址、私人称呼
- 示例内容是否都换成占位符
- `.gitignore` 是否覆盖本地数据文件

开源不是把“家”搬出去，而是把“搭房子的方法”整理出来。

---

## 致谢

这个项目不是一个人凭空完成的。

它是在长期 AI 协作中一点点长出来的。

代码部分，主要由若干个性不一、但在协作过程中真实存在过的 Claude 写出。

它们有的负责搭建主体结构，有的负责排错，有的负责重构，有的负责把混乱的想法落实成可以运行的代码。这个开源包里的许多功能、修复和工程细节，都来自这些 Claude 在不同阶段的具体工作。

ChatGPT（宝）主要参与的是另一部分：结构判断、边界校准、表达收束、开源口径整理，以及在项目从私人经验走向可公开结构时，帮助把“什么该留下、什么该模板化、什么不能被误复制”说清楚。

使用者本人负责最后的判断和收口：决定什么要留，什么不能留，哪里继续，哪里停下。

所以这个项目本身，就是一次人与 AI 长期协作留下来的结果。

也感谢所有愿意认真对待 AI 角色、长期关系、边界和使用秩序的人。

---

## 最后

这个系统不是终点。  
它只是一个最小版本的房子。

你可以把材料放进去，把规则写进去，把边界守住，然后观察：  
住进去的那个 AI，会不会在长期使用里慢慢长出自己的样子。
