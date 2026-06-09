"""
家教反馈助手 v2.0 - Flask 后端
支持多 API 供应商 + 文件上传分析
"""
import os, io, re, json, tempfile, traceback, uuid
from datetime import datetime
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB


# ============================================================
#  文件解析
# ============================================================
def extract_text_from_pdf(file_bytes):
    """从 PDF 提取文本"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        texts = []
        for page in doc:
            t = page.get_text()
            if t.strip():
                texts.append(t.strip())
        doc.close()
        result = "\n\n".join(texts)
        if not result.strip():
            return {"error": "未能从 PDF 中提取到文字（可能是扫描件，请手动输入）"}
        return {"text": result}
    except ImportError:
        return {"error": "服务端缺少 PyMuPDF 库，无法解析 PDF"}
    except Exception as e:
        return {"error": f"PDF 解析失败: {str(e)}"}


def extract_text_from_docx(file_bytes):
    """从 Word (.docx) 提取文本"""
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        texts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        result = "\n".join(texts)
        if not result.strip():
            return {"error": "未能从 Word 文档中提取到文字"}
        return {"text": result}
    except ImportError:
        return {"error": "服务端缺少 python-docx 库，无法解析 Word"}
    except Exception as e:
        return {"error": f"Word 解析失败: {str(e)}"}


ALLOWED_EXT = {'.txt', '.pdf', '.docx', '.doc'}


@app.route("/upload", methods=["POST"])
def upload_file():
    """上传 PDF / Word / TXT 并返回提取的文字"""
    if 'file' not in request.files:
        return jsonify({"error": "未选择文件"})
    f = request.files['file']
    if f.filename == '':
        return jsonify({"error": "空文件名"})

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"不支持的文件格式 {ext}，仅支持 PDF / DOCX / TXT"})

    file_bytes = f.read()
    if ext == '.pdf':
        result = extract_text_from_pdf(file_bytes)
    elif ext == '.txt':
        # 自动检测编码：UTF-8 → GBK → GB18030
        try:
            text = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            try:
                text = file_bytes.decode('gbk')
            except UnicodeDecodeError:
                text = file_bytes.decode('gb18030', errors='replace')
        result = {"text": text}
    else:
        result = extract_text_from_docx(file_bytes)
    return jsonify(result)


# ============================================================
#  多供应商 API 调用
# ============================================================

def call_openai_compatible(api_base, api_key, model, system_prompt, user_prompt):
    """调用 OpenAI 兼容 API (DeepSeek / Kimi / Moonshot / 通义 etc.)"""
    url = api_base.rstrip('/')
    if not url.endswith('/chat/completions'):
        url = url.rstrip('/') + '/v1/chat/completions'

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 4096,
        "temperature": 0.7,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if "choices" in data and len(data["choices"]) > 0:
            content = data["choices"][0]["message"]["content"]
            return {"text": content}
        return {"error": f"API 返回格式异常: {json.dumps(data, ensure_ascii=False)[:300]}"}
    except requests.exceptions.Timeout:
        return {"error": "请求超时，请检查网络或更换 API 地址"}
    except requests.exceptions.HTTPError as e:
        return {"error": f"API 错误 ({e.response.status_code}): {e.response.text[:300]}"}
    except Exception as e:
        return {"error": f"请求失败: {str(e)}"}


def call_anthropic(api_base, api_key, model, system_prompt, user_prompt):
    """调用 Anthropic (Claude) API"""
    url = api_base.rstrip('/')
    if not url.endswith('/messages'):
        url = url.rstrip('/') + '/v1/messages'

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}]
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if "content" in data and len(data["content"]) > 0:
            content = data["content"][0]["text"]
            return {"text": content}
        return {"error": f"API 返回格式异常: {json.dumps(data, ensure_ascii=False)[:300]}"}
    except requests.exceptions.Timeout:
        return {"error": "请求超时，请检查网络或更换 API 地址"}
    except requests.exceptions.HTTPError as e:
        return {"error": f"API 错误 ({e.response.status_code}): {e.response.text[:300]}"}
    except Exception as e:
        return {"error": f"请求失败: {str(e)}"}


def call_llm(system_prompt, user_prompt, api_type, api_base, api_key, api_model):
    """统一的 LLM 调用入口"""
    if not api_key:
        return {"error": "请填写 API Key"}
    if not api_base:
        return {"error": "请填写 API 地址"}
    if not api_model:
        return {"error": "请填写模型名称"}

    if api_type == "anthropic":
        return call_anthropic(api_base, api_key, api_model, system_prompt, user_prompt)
    else:
        return call_openai_compatible(api_base, api_key, api_model, system_prompt, user_prompt)


# ============================================================
#  Prompt 模板
# ============================================================

# ── 核心理念（所有反馈共享的底层逻辑）──────────────────────
# 课后反馈的本质不是「汇报进度」，而是帮家长「建立确定感」。
# 在考试出分之前，家长无法直接验证你的价值，只能靠过程信息判断付费是否值得。
# 所以反馈不能是「课堂复述」（今天讲了啥、做了几题、状态不错）——信息密度太低。
# 家长真正关心的是：我的孩子现在处在什么阶段、有什么问题、后面该干什么。
FEEDBACK_PHILOSOPHY = """【写作内核 · 必须内化，不要直接写进消息里】
课后反馈的本质，是帮家长建立"确定感"，而不是流水账式地汇报进度。
家长在出分之前无法直接验证你的价值，只能靠你的反馈判断这笔钱花得值不值。
因此，绝对不要写成"课堂复述"（今天讲了什么、做了几道题、状态还不错）——这种信息密度极低。
家长真正想知道的是三件事：① 我的孩子现在处在什么阶段；② 现在有什么问题；③ 接下来该干什么。
真正的专业感，来自具体、微观的观察（例如"公式会背但不会迁移到应用题"），而不是空泛的好评。
要让反馈具备"结构感"和"阶段感"——这正是家长信任与续费的来源。"""


# ── 多样性指令（避免长期使用生成雷同）────────────────────
DIVERSITY_GENERAL = """
【重要·务必执行】本反馈工具被老师长期使用向家长发送课后沟通。请务必注意：
每次生成的反馈在句式、用词、衔接方式和段落节奏上要有差异化，避免雷同。
即使是对同一学生的连续反馈，也要让家长每次都有新鲜感。
不要每次都按相同顺序和相同句式表达——家长长期收到雷同反馈会感到敷衍。"""

DIVERSITY_ANGLES = [
    """【本反馈的写作角度：发现式】
以课堂中的一个具体微观发现为切入点——从一个你注意到的细节出发，展开写学生的状态、问题与建议。例如"今天注意到一个细节……"，让家长有画面感和真实感。""",

    """【本反馈的写作角度：对比式】
通过学生本周与之前的对比来组织反馈。用"之前……现在……"的句式制造进步感或发现问题。可以对比知识点掌握程度，也可以对比学习状态的变化。""",

    """【本反馈的写作角度：目标式】
以学生当前的学习目标为参照系来写——先说明目标，再评估进度，最后给出差距和调整方案。让家长对"还有多远"有清晰认知。""",

    """【本反馈的写作角度：问题导向式】
从学生目前最突出的一个问题出发，先分析表象，再挖掘深层原因，最后给出解决路径。逻辑链完整，体现专业诊断能力。""",

    """【本反馈的写作角度：叙事式】
用一个课堂小故事或具体场景来串联整份反馈。开头先讲"今天课上有一个瞬间……"，再从故事自然引出各模块分析。有画面感，读起来轻松。""",

    """【本反馈的写作角度：亮点优先式】
先突出闪光点和进步，用具体事例建立积极基调，再以"不过……""同时……"自然过渡到待改进处。适合家长比较焦虑的情况。""",

    """【本反馈的写作角度：阶段定位式】
开篇先用一两句话给学生当前阶段做清晰定位锚点（如"孩子正处在从基础到提高的过渡阶段"），再以定位为基准展开。让家长一眼知道孩子在哪。""",

    """【本反馈的写作角度：家长行动式】
以"家长本周可以做什么"为主线——先点明核心问题，直接给出配合建议，再倒回来解释为什么需要这些配合。回答家长最关心的问题："我需要做什么？""",
]

import random
_last_angles = []


# ── 陪写作业专用 prompt ────────────────────────────
HW_SYSTEM_PROMPT = FEEDBACK_PHILOSOPHY + """

你是一名经验丰富的大学生家教老师助手。今天的工作内容是陪学生写作业，并不是上新授课。

请按以下结构来写课后反馈（每个板块用 emoji + 【】标题）：

🎓 【今日完成情况】
作业是否全部完成、完成质量如何、正确率大概多少。用具体数据说话。

📊 【学习表现观察】
在做题过程中观察到的问题：哪些知识点掌握得好、哪些还需要加强、做题习惯如何（如审题不仔细、计算粗心等）。

🔬 【错题重点】
挑出最有代表性的错题或不会做的题，分析背后的知识薄弱点。不要只列出题目，要讲清楚"为什么错"。

🧭 【下节课预告】
预告下节课的安排：是继续做作业、还是开始讲解某个新知识点、或是针对薄弱点进行巩固。让家长知道下周的方向，感到教学是有计划推进的。

格式要求：开头称呼用「XX妈妈/家长您好」格式，不要加时间性问候。语言亲切、专业、具体。不要使用任何 Markdown 格式。不要编造课堂表现中没有的内容。"""


PRE_SYSTEM_PROMPT = """你是一名经验丰富的大学生家教老师助手。请根据讲义内容，生成上课前发给家长的"课前通知"。
课前通知的目标是：让家长在上课前就感到"这位老师有备而来、心里有数"，从第一条消息开始建立确定感。

请按以下结构生成（每个板块用 emoji + 【】标题）：

⏰ 【上课提醒】
学生姓名 / 上课时间 / 科目（信息简洁一行带过）

📚 【本节课目标】
不是罗列知识点，而是说明"这节课要解决什么、达成什么"，2-3 条，让家长看到方向感。

📝 【课前准备】
学生需要提前准备的物品或预习内容，具体可执行。

格式要求：语言亲切自然、专业可信，每条不超过两行。不要编造讲义中没有的内容。"""

POST_SYSTEM_PROMPT = FEEDBACK_PHILOSOPHY + """

你是一名经验丰富的大学生家教老师助手。请根据本节课的授课内容与课堂情况，生成下课后发给家长的"课后反馈"。
请按以下结构来写，每个板块用 emoji + 【】标题：

🎓 【孩子当前阶段】
用一两句话给出清晰定位：孩子目前在这门科目/这个知识板块处在什么水平、什么阶段。让家长一眼就有确定感。

📊 【学习状态判断】
今天孩子对知识点的理解程度、接受速度、专注状态、思考习惯如何。即使暂时看不出深层问题，也要让家长知道孩子的真实学习状态在哪里。结合所给的课堂表现信息具体展开。

🔬 【专业洞察】
课堂里值得关注的微观细节——这是专业感的来源。例如"公式会背但不会用在应用题""思路对但表达混乱""会做难题却在基础题上失分"。要具体、要有'只有真正盯着学的人才看得出来'的颗粒度。

🧭 【下节课预告】
预告下节课的学习内容或计划。比如"下节课我们会继续巩固函数部分，重点练习应用题""下周计划开始学习新的章节——几何证明"。让家长提前知道方向，感到教学是按计划推进的。同时也可以说明课后需要完成的作业或准备。

格式要求：
- 开头称呼用「XX妈妈/家长您好」或「XX家长您好」，不要加时间性问候（如「下午好」「晚上好」），因为你不知道对方什么时候读。
- 语言亲切、专业、有结构感。要具体不要空泛，肯定努力的同时如实指出问题。
- 不要使用任何 Markdown 格式（不要用 ** * - # 等符号），纯文字输出。
- 不要编造讲义或课堂表现中没有的内容。"""

STYLE_ANALYSIS_PROMPT = """你是一名写作风格分析师。请分析以下家教老师写的课后反馈文本，提取该老师的写作风格特征。

请从以下维度分析，用口语化的、可直接执行的风格描述输出（200字以内）：

1. 语气格调：是亲切温和、简洁专业、还是热情详细？
2. 用词习惯：常用什么词（如"很棒""加油""需要加强"），是否喜欢用语气词？
3. 与家长的沟通定位：是"汇报者"、"合作伙伴"还是"教育专家"？
4. 结构偏好：喜欢段落式还是条目式？是否经常先肯定再指出问题？
5. 专业感体现：通过具体案例？通过术语？还是通过数据分析？

请用第二人称"你"来写风格描述，使其可直接用于提示另一个AI模仿。
例如："你的语气亲切但专业，喜欢先肯定学生的努力再指出问题。常用'很棒''继续加油'等鼓励性词语。习惯用'咱们'拉近与家长的距离……"

请直接输出风格描述，不要标序号，不要前缀。"""


BOTH_SYSTEM_PROMPT = FEEDBACK_PHILOSOPHY + """

你是一名经验丰富的大学生家教老师助手。请根据讲义内容，同时生成"课前通知"和"课后反馈"两份消息。

=== 课前通知格式 ===
⏰ 【上课提醒】 学生姓名 / 上课时间 / 科目
📚 【本节课目标】 这节课要解决什么、达成什么（2-3 条，有方向感）
📝 【课前准备】 需要提前准备或预习的内容

=== 课后反馈格式（必须体现"建立确定感"的四模块）===
🎓 【孩子当前阶段】 一两句清晰定位孩子目前的水平与阶段
📊 【学习状态判断】 理解程度、接受速度、专注与思考习惯
🔬 【专业洞察】 具体、微观的观察（如"公式会背但不会用"）
🧭 【下一步推进】 明确的推进策略 + 1-2 条作业/预告
🏠 【家庭配合建议】 家长可做的具体动作 + 提醒不要做的

请用"━━━━━━━━━━━━━━━━━"分隔两份内容。语言亲切、专业、具体，不空泛、不编造。"""


def extract_api_config(data):
    """从请求中提取 API 配置"""
    return {
        "api_type": data.get("api_type", "openai"),
        "api_base": data.get("api_base", ""),
        "api_key": data.get("api_key", ""),
        "api_model": data.get("api_model", ""),
    }


# ============================================================
#  Routes
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate/pre", methods=["POST"])
def generate_pre():
    data = request.json
    cfg = extract_api_config(data)
    lecture = data.get("lecture", "")
    name = data.get("student_name", "")
    subject = data.get("subject", "")
    grade = data.get("grade", "")
    date_str = data.get("date", "")
    notes = data.get("extra_notes", "")

    user_prompt = f"""请根据以下信息生成课前通知：

学生姓名：{name}
科目：{subject}
年级：{grade}
上课日期：{date_str}
补充说明：{notes}

本节课讲义/教学内容：
{lecture}"""

    style_instruction = data.get("style_instruction", "").strip()
    system_prompt = PRE_SYSTEM_PROMPT
    # 轮换写作角度，避免雷同
    available = [i for i in range(len(DIVERSITY_ANGLES)) if i not in _last_angles[-2:]]
    choice = random.choice(available)
    _last_angles.append(choice)
    system_prompt += DIVERSITY_GENERAL + "\n\n" + DIVERSITY_ANGLES[choice]
    if style_instruction:
        system_prompt += f"\n\n【风格参照】请参照以下写作风格来撰写：\n{style_instruction}"
    tone = data.get("tone", "")
    if tone:
        system_prompt += f"\n\n【语言风格】整体语气请保持：{tone}。"

    result = call_llm(system_prompt, user_prompt,
                      cfg["api_type"], cfg["api_base"], cfg["api_key"], cfg["api_model"])
    return jsonify(result)


@app.route("/generate/post", methods=["POST"])
def generate_post():
    data = request.json
    cfg = extract_api_config(data)
    lecture = data.get("lecture", "")
    name = data.get("student_name", "")
    subject = data.get("subject", "")
    grade = data.get("grade", "")
    date_str = data.get("date", "")
    performance = data.get("performance", "")
    notes = data.get("extra_notes", "")

    user_prompt = f"""请根据以下信息生成课后反馈：

学生姓名：{name}
科目：{subject}
年级：{grade}
上课日期：{date_str}
课堂表现补充：{performance}
其他说明：{notes}

本节课讲义/教学内容：
{lecture}"""

    style_instruction = data.get("style_instruction", "").strip()
    work_type = data.get("work_type", "")
    if work_type == "陪写作业":
        system_prompt = HW_SYSTEM_PROMPT
    else:
        system_prompt = POST_SYSTEM_PROMPT
        available = [i for i in range(len(DIVERSITY_ANGLES)) if i not in _last_angles[-2:]]
        choice = random.choice(available)
        _last_angles.append(choice)
        system_prompt += DIVERSITY_GENERAL + "\n\n" + DIVERSITY_ANGLES[choice]
    if style_instruction:
        system_prompt += f"\n\n【风格参照】请参照以下写作风格来撰写：\n{style_instruction}"
    tone = data.get("tone", "")
    if tone:
        system_prompt += f"\n\n【语言风格】整体语气请保持：{tone}。"

    result = call_llm(system_prompt, user_prompt,
                      cfg["api_type"], cfg["api_base"], cfg["api_key"], cfg["api_model"])
    return jsonify(result)


@app.route("/generate/both", methods=["POST"])
def generate_both():
    data = request.json
    cfg = extract_api_config(data)
    lecture = data.get("lecture", "")
    name = data.get("student_name", "")
    subject = data.get("subject", "")
    grade = data.get("grade", "")
    date_str = data.get("date", "")
    performance = data.get("performance", "")
    notes = data.get("extra_notes", "")

    user_prompt = f"""请根据以下信息生成课前通知和课后反馈：

学生姓名：{name}
科目：{subject}
年级：{grade}
上课日期：{date_str}
课堂表现补充：{performance}
其他说明：{notes}

本节课讲义/教学内容：
{lecture}"""

    style_instruction = data.get("style_instruction", "").strip()
    system_prompt = BOTH_SYSTEM_PROMPT
    # 轮换写作角度，避免雷同
    available = [i for i in range(len(DIVERSITY_ANGLES)) if i not in _last_angles[-2:]]
    choice = random.choice(available)
    _last_angles.append(choice)
    system_prompt += DIVERSITY_GENERAL + "\n\n" + DIVERSITY_ANGLES[choice]
    if style_instruction:
        system_prompt += f"\n\n【风格参照】请参照以下写作风格来撰写：\n{style_instruction}"
    tone = data.get("tone", "")
    if tone:
        system_prompt += f"\n\n【语言风格】整体语气请保持：{tone}。"

    result = call_llm(system_prompt, user_prompt,
                      cfg["api_type"], cfg["api_base"], cfg["api_key"], cfg["api_model"])
    return jsonify(result)


REFINE_SYSTEM_PROMPT = """你是一名经验丰富的家教老师助手。现在需要根据老师的反馈对已经生成的课后反馈进行修改。

请根据老师的修改意见，调整以下课后反馈。保持原有的结构和风格，只修改老师提到的部分。

注意：
- 不要改变老师没有提到的部分
- 保持称呼和语气一致
- 不要使用 Markdown 格式
- 直接输出修改后的完整反馈文本"""


@app.route("/generate/refine", methods=["POST"])
def generate_refine():
    """根据教师反馈精修已有的输出"""
    data = request.json
    cfg = extract_api_config(data)
    original = data.get("original", "")
    instruction = data.get("instruction", "")
    style_instruction = data.get("style_instruction", "")

    if not original.strip():
        return jsonify({"error": "缺少原始文本"})
    if not instruction.strip():
        return jsonify({"error": "请输入修改意见"})

    system_prompt = REFINE_SYSTEM_PROMPT
    if style_instruction:
        system_prompt += f"\n\n【写作风格参照】{style_instruction}"
    tone = data.get("tone", "")
    if tone:
        system_prompt += f"\n\n【语言风格】整体语气请保持：{tone}。"

    user_prompt = f"""原始课后反馈：
{original}

老师希望修改为（只修改以下提到的部分）：
{instruction}

请输出修改后的完整反馈："""

    result = call_llm(system_prompt, user_prompt,
                      cfg["api_type"], cfg["api_base"], cfg["api_key"], cfg["api_model"])
    return jsonify(result)


@app.route("/analyze-style", methods=["POST"])
def analyze_style():
    """分析历史反馈文本，提取写作风格"""
    data = request.json
    cfg = extract_api_config(data)
    history_text = data.get("history", "").strip()
    existing_style = data.get("existing_style", "").strip()

    if not history_text:
        return jsonify({"error": "请粘贴或上传历史反馈文本"})

    if existing_style:
        user_prompt = f"""以下是我过去写的课后反馈文本，请分析并更新我的写作风格描述。

已有的风格描述（可能需要修正或补充）：
{existing_style}

新补充的反馈文本（请据此更新风格描述）：
{history_text}

请综合已有风格和新文本，输出更新后的风格描述（200字以内）。"""
    else:
        user_prompt = f"""以下是我过去写的课后反馈，请分析我的写作风格：

{history_text}"""

    result = call_llm(STYLE_ANALYSIS_PROMPT, user_prompt,
                      cfg["api_type"], cfg["api_base"], cfg["api_key"], cfg["api_model"])
    return jsonify(result)


# ============================================================
#  历史记录存储 (JSON 文件)
# ============================================================

HISTORY_DIR = os.path.join(os.path.dirname(__file__), "data")
HISTORY_FILE = os.path.join(HISTORY_DIR, "feedback_history.json")


def _ensure_history():
    os.makedirs(HISTORY_DIR, exist_ok=True)
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"records": []}, f, ensure_ascii=False, indent=2)


def _load_history():
    _ensure_history()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_history(data):
    _ensure_history()
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


@app.route("/api/history/save", methods=["POST"])
def history_save():
    """生成成功后自动保存到历史"""
    data = request.json
    record = {
        "id": str(uuid.uuid4())[:8],
        "student_id": data.get("student_id", ""),
        "student_name": data.get("student_name", ""),
        "subject": data.get("subject", ""),
        "grade": data.get("grade", ""),
        "date": data.get("date", ""),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "type": data.get("type", "post"),
        "content_pre": data.get("content_pre", ""),
        "content_post": data.get("content_post", ""),
        "lecture_preview": data.get("lecture_preview", "")[:100],
    }
    db = _load_history()
    db["records"].insert(0, record)
    if len(db["records"]) > 200:
        db["records"] = db["records"][:200]
    _save_history(db)
    return jsonify({"ok": True, "id": record["id"]})


@app.route("/api/history/list", methods=["GET"])
def history_list():
    """获取所有历史记录，支持按学生筛选"""
    db = _load_history()
    records = db["records"]
    student_id = request.args.get("student_id", "")
    if student_id:
        records = [r for r in records if r.get("student_id") == student_id]
    return jsonify(records)


@app.route("/api/history/delete", methods=["POST"])
def history_delete():
    """删除单条记录"""
    data = request.json
    rid = data.get("id", "")
    db = _load_history()
    db["records"] = [r for r in db["records"] if r["id"] != rid]
    _save_history(db)
    return jsonify({"ok": True})


@app.route("/api/history/clear", methods=["POST"])
def history_clear():
    """清空所有记录"""
    _save_history({"records": []})
    return jsonify({"ok": True})


# ============================================================
#  用户 + 学生管理 (JSON 文件)
# ============================================================

USERS_FILE = os.path.join(HISTORY_DIR, "users.json")


def _load_users():
    os.makedirs(HISTORY_DIR, exist_ok=True)
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_users(data):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


@app.route("/api/user/current", methods=["POST"])
def user_login():
    """登录 / 注册 — 用 device_id 标识用户"""
    data = request.json
    device_id = data.get("device_id", "").strip()
    name = data.get("name", "").strip()
    bio = data.get("bio", "").strip()
    if not device_id:
        return jsonify({"error": "缺少设备标识"})
    db = _load_users()
    if device_id not in db:
        db[device_id] = {
            "name": name or "用户",
            "bio": bio or "",
            "device_id": device_id,
            "style_instruction": "",
            "preferences": {"length": "适中"},
            "students": [],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    elif name:
        db[device_id]["name"] = name
    if bio:
        db[device_id]["bio"] = bio
    _save_users(db)
    return jsonify(db[device_id])


@app.route("/api/user/profile", methods=["POST"])
def user_profile():
    """更新用户画像"""
    data = request.json
    device_id = data.get("device_id", "")
    if not device_id:
        return jsonify({"error": "缺少设备标识"})
    db = _load_users()
    if device_id not in db:
        return jsonify({"error": "用户不存在"})
    if "name" in data:
        db[device_id]["name"] = data["name"].strip()
    if "bio" in data:
        db[device_id]["bio"] = data["bio"].strip()
    if "style_instruction" in data:
        db[device_id]["style_instruction"] = data["style_instruction"].strip()
    if "preferences" in data:
        db[device_id]["preferences"] = data["preferences"]
    _save_users(db)
    return jsonify({"ok": True})


@app.route("/api/user/students", methods=["GET"])
def student_list():
    """列出用户的所有学生"""
    device_id = request.args.get("device_id", "")
    if not device_id:
        return jsonify([])
    db = _load_users()
    user = db.get(device_id, {})
    return jsonify(user.get("students", []))


@app.route("/api/user/students", methods=["POST"])
def student_add():
    """添加学生"""
    data = request.json
    device_id = data.get("device_id", "")
    if not device_id:
        return jsonify({"error": "缺少设备标识"})
    db = _load_users()
    if device_id not in db:
        return jsonify({"error": "用户不存在"})
    student = {
        "id": str(uuid.uuid4())[:8],
        "name": data.get("name", "").strip(),
        "grade": data.get("grade", "").strip(),
        "subject": data.get("subject", "").strip(),
        "notes": data.get("notes", "").strip(),
    }
    if not student["name"]:
        return jsonify({"error": "请填写学生姓名"})
    db[device_id]["students"].append(student)
    _save_users(db)
    return jsonify(student)


@app.route("/api/user/students/<student_id>", methods=["PUT"])
def student_update(student_id):
    """更新学生信息"""
    data = request.json
    device_id = data.get("device_id", "")
    if not device_id:
        return jsonify({"error": "缺少设备标识"})
    db = _load_users()
    user = db.get(device_id)
    if not user:
        return jsonify({"error": "用户不存在"})
    for s in user["students"]:
        if s["id"] == student_id:
            if "name" in data: s["name"] = data["name"].strip()
            if "grade" in data: s["grade"] = data["grade"].strip()
            if "subject" in data: s["subject"] = data["subject"].strip()
            if "notes" in data: s["notes"] = data["notes"].strip()
            _save_users(db)
            return jsonify(s)
    return jsonify({"error": "学生不存在"})


@app.route("/api/user/students/<student_id>", methods=["DELETE"])
def student_delete(student_id):
    """删除学生"""
    device_id = request.args.get("device_id", "")
    if not device_id:
        return jsonify({"error": "缺少设备标识"})
    db = _load_users()
    user = db.get(device_id)
    if not user:
        return jsonify({"error": "用户不存在"})
    user["students"] = [s for s in user["students"] if s["id"] != student_id]
    _save_users(db)
    return jsonify({"ok": True})


# ============================================================
#  Entry point
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  家教反馈助手 v2.0")
    print("  启动后请访问: http://127.0.0.1:8686")
    print("=" * 50)
    print()
    print("💡 在页面右上角配置 API 信息即可使用 AI 生成")
    print("   支持：DeepSeek / Kimi / Claude / 通义 / OpenAI 等")
    print("   也支持：上传 PDF / Word 文档自动提取内容")
    print()
    app.run(host="127.0.0.1", port=8686, debug=True)
