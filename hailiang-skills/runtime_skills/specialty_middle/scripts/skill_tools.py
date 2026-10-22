#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill_tools.py · 初中特长推荐 skill v3.5.4 查询层 + 规则引擎

作用：模型在对话中调用它查询机械数据（标签/方向池/说明/赛事/优势/起步/必要条件判定）
     并完成推荐收敛计算，返回 JSON 供模型逐字采用，根除"凭印象编造"。

数据源（按大类×数据类型拆为细粒度库）：
     references_01 特质标签与方向映射——启动时解析（标签/线索/判别/方向池/
     方向→大类路由表/俗称对照）；其余 30 个细粒度库 = 6 大类（数理/科创/
     艺术/体育/国际/人文）× 5 数据类型（必要条件/简要说明/赛事/优势/起步），
     文件名 references_{大类简称}_{数据类型}.md（如 references_数理_赛事.md），
     每文件只含一个大类×一类数据。**查询哪个方向哪类数据，脚本只解析对应的
     那一个小文件**（细粒度懒加载 + 缓存）；full_packages 按涉及方向的大类加载
     必要条件（全部候选判定用）+ kept 方向的其余四类；赛事名全局检索时才加载
     全部 6 个赛事库。改某大类某类数据只改对应小文件、不动代码。
     判定引擎（窗口两层、五科路线、可行度分级、上限3收敛）内嵌于本文件，
     口径文本一律取自所属大类必要条件库「判定口径 / 不满足时口径 / 待问句」列。

用法（平台约定：stdin JSON → stdout JSON）：
    平台：  平台把本轮 JSON 上下文经 stdin 传入（{"action": 操作名, …参数}，
            或嵌套对象 / 含 skill_tools.操作名({...}) 文本标记的任意上下文），
            脚本宽容解析后执行一个操作，stdout 只返回一个 JSON 对象。
    调试：  python skill_tools.py <op> '<json_args>' ｜ 冒烟：python skill_tools.py --smoke
历史：
    v3.7.1  collect 首轮三段结构 + 问句分两轮（2026-09-22 用户第三轮
            测试反馈：流程/取数/赛事优势已准确，两点呈现优化：①标签
            确认后的 collect 轮直接列了具体方向，应改给"大方向"定性
            （选①→数理方向）+ 说明问条件是为了帮筛出合适方向；
            ②ask_union 6 问一轮全发太长，应分两轮、每轮关联性强）：
            ① 「指令」字段重写（脚本逻辑零改动，仅文案）：collect 轮
            三段结构——大方向定性一句（标签→大类对照 ①数理/②科创/
            ③体育/④艺术/⑤国际/⑥人文，只说大类禁列具体方向名）+
            提问目的一句 + 问句；"禁止输出任何结论"放宽为"禁止输出
            具体方向推荐结论"（大方向定性是标签确认的自然延伸）；
            ② 问句 >3 个分两轮（第一轮孩子的情况：安全置顶+身体条件
            +学习经历；第二轮家里的情况：城市/投入），≤3 一轮问完；
            ③ 指令同步提示：盘点已确认的学习经历先进 facts（学过的
            带起学/时长，没学过的方向带零基础）——避免对没学过的孩子
            发"几年级开始学"预设性问句；④ SKILL.md 同步：M5 collect
            首轮三段结构+分组规则+盘点 facts 前置核检、〇.5 组合表
            B/C/D 行、七/八/九节引用行、G8-14 自检项（零具体方向结论
            +拆轮+盘点 facts 映射）。
    v3.7.0  去 force_final 开关 + 标签轮/归类轮/selected_tag 形态适配 +
            展示指引双分支（2026-09-22 用户两份新测试日志反馈：①模型一轮
            就带 force_final:true 把两阶段锁解锁、跳过提问直接出结论（文档
            禁令管不住）；②标签轮平台传 tags:[]（空数组）落
            no_action_detected → 模型自编问题、选项展示不全；③家长选标签
            ⑥后参数只带 selected_tag:"6"、归类轮只带 grade+interest_clue
            → 全落 no_action_detected → 模型拿不到池/问句/标签原文）：
            ① 删除 force_final 参数（含 planner 层透传）：phase 只由
            tags+ask_union 决定；唯一出口 = 家长作答（值进 facts）或
            答"不清楚/没有/不打算提供"（原话进 facts，_unknown 词表
            命中 → 按已答未知处理、问句不回填）→ 重调自动 phase=final；
            模型想跳过提问只能往 facts 编造值，日志可审计；
            ② planner ④标签轮形态：tags:[] 空数组 → 返回 tags 操作
            （6 标签 + 展示指引）；③ planner ③标签池形态收录
            selected_tag/selected_tags（"6"/⑥/P6/int、单选多选同权，
            日志2第6轮实测）；④ planner ⑤归类轮兜底：年级真实值 +
            无 stage 声明 + ①-④全不中 → tags 操作（日志2第4轮实测，
            此前模型凭记忆展示残缺选项）；stage=前置采集/collect 盘点轮
            与首轮占位符不喂标签数据，防跳过年级/盘点轮；⑤ op_tags
            展示指引双分支：首次展示=逐条+兜底邀约，已展示过=禁止重发、
            改用 clues 归类确认；⑥ smoke 99/99（新增：展示指引已展示
            分支、selected_tag→P6 池 collect、selected_tags 混写、
            归类轮兜底、盘点轮/首轮不喂数据防跳步）。
    v3.6.0  两阶段协议 + 第三套 facts 键名 + 数理赛事库同步（2026-09-22
            用户两份测试日志反馈：①推荐流程标签确认后未问必要条件直接出
            结论表——平台传参/ask_union 都正确，病根是模型拿到"问句+完整
            结论表"一整包后直接输出结论；②美术 F→G 轮整版重贴已示块且
            "条件判断"是模型编造——病根是平台第三套 facts 键名
            （年投入/孩子年级/色觉正常:布尔）未适配，家长答完的条件全丢、
            gate 卡待信息重发已答问题）：
            ① `full_packages` 两阶段锁：标签池形态（tags 存在）下 ask_union
            非空且未带 force_final → phase="collect"，只返回问句（kept 只留
            name/gate，剥除 brief/competitions/advantage/steps）+「指令」
            字段——模型无结论可提前输出；条件齐或问句清空 → phase="final"
            全版块；单方向形态（无 tags）不锁，v3.5.3 SOFT_MISSING 初步
            结论口径不变，出参加 notice（判定轮只输出判定段、已示块不重贴）；
            ② force_final 出口：问满 2 轮仍未答/家长拒答 → 模型带
            "force_final":true 重调取终版（planner 标签池形态同步透传），
            仍有未答时出参带「终版口径」（未答项按待补充呈现、禁编造）；
            ③ `_planner_facts` 第三套键名：年投入/年度投入→教育投入、
            孩子年级/学生年级/在读年级→grade、布尔形态（色觉正常:true→
            色觉:正常，false→色弱；听力/心肺同构，False 走异常值宁严勿漏；
            家庭规划/读写三项 bool 直存）+ 前瞻键：成绩位置/成绩/
            academic_performance→成绩（数理 ask_union 问成绩位置、平台提取
            键名未实测，别名与契约键名双收）+ GLOBAL_FACT_KEYS 契约键名
            直传（家庭规划/色觉/听力/读写三项，占位符忽略）；
            ④ 数理赛事库同步用户 2026-09-22 更新（删 7 条：数学华罗庚金杯/
            希望杯/迎春杯/走进美妙的数学花园、信息学蓝桥杯、化学天原杯/
            IChO 选拔赛，数学 8→4 条）；⑤ smoke 89/89（新增/改写断言：两阶段
            锁、force_final 终版、第三套键名落地/布尔 False、日志A第4轮=
            可行零追问、日志B collect→final 闭环回放、成绩位置/家庭规划
            前瞻键、已删赛事不再命中、数学计数 4）；⑥ SKILL.md：M5 改两
            阶段协议（collect 零结论/final 出终版/force_final 出口）、
            接口表/〇.5 组合行/B·C·D 骨架行同步、十一节 notice 引用、
            G8-14（collect 零结论 + force_final 时机）。
    v3.5.4  优势分级呈现 + 图3场景防回归（2026-09-21 用户反馈四问题：多方向
            结论应输出优势"总述"而非全量维度分点；输出应先优势后赛事；平台
            实测存在优势/赛事未取库、身体信息漏问）：
            ① `direction_advantage` / `full_packages` 出参新增 `summary` 字段
            （优势库「维度=总述」行原文；无总述方向=机器人/科学创新/科技体育
            等 summary=null）——SKILL.md M3 改分级呈现：多方向结论轮（kept≥2）
            每方向只给总述一行、禁自编禁越级展开，单方向场景（kept=1、F1、
            G-库内、点名展开）仍全量维度分点；② SKILL.md 〇.5/七/八/九/F1/
            G-库内/十三节/G8 全线同步"优势块（M3）在赛事块（M2）之前"顺序
            铁律 + M2 单赛事方向（田径/武术 display 仅 1 条）禁凑数禁补常见
            赛事（多写一条即编造）；③ 图3场景（初三 P3+P5+篮球兴趣零基础）
            防回归断言 6 项：P5 代表必入选（禁全体育自选）、体育方向
            missing_required 必含安全类条件（心肺）、心肺问句置顶 ask_union、
            summary=总述原文/无总述=null、多方向包内含 summary——脚本侧
            收敛/置顶逻辑 v3.1.x/v3.5.2 已备，平台漏问属模型未按出参输出，
            断言锁死防回归。
            同日第二轮（推荐流程完整日志：标签池/操作名形态 planner 调用
            no_action_detected → 结论轮拿不到池/优势/赛事/ask_union，模型
            转而自问"更倾向哪一个"；三年级零基础被判"窗口必不满足"与库
            口径冲突；首轮问句混入特质邀约）：
            ④ `_planner_call` 补全三形态——操作名形态（parameters 内或顶层
            operation/action 键，如 {"operation":"tags"}）、单方向形态（v3.4.1
            既有）、标签池形态（tags 存在、无 direction（P3/③/3 写法皆可）→
            full_packages 池补全多方向包；零基础是"孩子什么都没学过"的孩子级
            事实 → 扇出给全部池方向，已学时长/起学年级等方向语境键无方向
            绑定不扇出；平台 items 是版块名数组不是方向列表，不透传）；
            ⑤ `_window_check` 区分学段：小学在读零基础 = 现在起步、起学仍在
            窗口内（不再追问起学/持续/柔韧），初中在读（含年级未知从严）零
            基础才按"窗口必不满足"受限；⑥ `_planner_facts` 键表补
            "以前学过/学过/曾学过"（值含"没有/无"类=零基础）；⑦ SKILL.md：
            info 首问只问年级（特质邀约"说说孩子平时怎么样，我来帮您判断"
            固定属 3.1 标签轮，G8-13 锁死）、前置采集改分轮（年级轮→盘点轮
            →盘点补问轮，最多 3 轮）、六年级及以下不交回改"方向性参考"
            口径（照常取数判定+小学说明句）；⑧ smoke +6 断言（操作名形态×2、
            标签池形态 ask_union+全版块真实日志回放、小学零基础窗口过不追问、
            初中零基础受限、方向语境键不打底改无 tags 双 items 确定性命中）
    v3.5.3  初步结论分级（2026-09-18 游泳对话日志：家长答完全部条件、仅臂展
            "没量过"（已答未知）→ verdict 停在待信息，模型只能自己违规给初步
            判断；用户要求：仅非关键项缺时给"初步判断合适，但还需要…"）：
            verdict 判定加 SOFT_MISSING 白名单（臂展/教育投入/家庭规划/
            成绩位置/读写三项）——缺项全部 ∈ 白名单 → 升级为有条件可行 +
            limits 追加「初步判断」口径条（"其余条件满足，X待确认——按已知
            信息初步判断本方向可行"），missing_required 照登（待补充呈现）；
            核心缺项（安全类/起学窗口/竞赛路线年级开关）仍按待信息，安全与
            窗口没有初步判断的余地
    v3.5.2  跨流程承接复查（2026-09-18）：full_packages 新增**顶层 facts**（全局
            通用项）——白名单键（城市/教育投入/心肺/色觉/听力/成绩/家庭规划/
            读写三项/年级）打底到全部候选（含池自动补全方向），item 级同名键
            覆盖；零基础/已学时长/起学年级/持续训练/柔韧/臂展等**方向语境键
            不打底**（游泳零基础≠美术零基础，防止污染窗口判定）。修复跨流程
            重复提问的脚本侧通道：G/F 阶段家长已答的通用项，转 A 主流程后
            池补全候选（机器人/思辨等）不再重复追问城市/教育投入
    v3.5.1  平台 planner facts 键表扩展（2026-09-18 游泳对话日志定位：平台实际
            提取 health/learning_start/arm_span/investment/city，旧适配只认
            physical_condition/prior_experience/grade 三键 → 家长五答全丢、
            gate 永远待信息+ask 5问满额、模型每轮重贴同一份 882 字结论）：
            心肺值归一加否定模式（"无身体问题/没有哮喘/医生未禁止"→无碍，
            命不中才原样传、按受限口径处理——安全类宁严勿漏）；零基础词表补
            "未学过"（平台用"未"、家长口语用"没"）；新增起学年级提取；
            investment→教育投入、city→城市、arm_span→臂展达标（未测类=
            已答未知原样传入不追问）三组新键；_UNKNOWN_WORDS 补 不知道/
            不确定/未测/没测过
    v3.5.0  ①取消"英语能力培养/综合能力培养"两方向（references 全库与
            STANDARD_DIRECTIONS 同步删除，21 方向）；②阅读素养补齐（软性素养项
            读写耐力/共情理解/读写致用——不满足只降可行度、方向保留；赛事/
            优势/起步数据齐全；俗称「读写素养」经表七归一）；③追问规则引擎化：
            家长明确回答"不清楚/无法确定"类（_unknown 短语表，≤12 字）= 已答
            未知——missing_required 照登（待信息呈现）但问句不进 ask /
            ask_union，禁止再追问（安全/窗口/臂展/色觉/听力/家庭规划/成绩/
            教育投入/软性项全分支覆盖）；④入口协议补 MCP 风格嵌套形态：
            {"tool":操作名,"args":{…}} 及 {"skill_tools":{"tool":…,"args":…}}
            包装（_extract_op_args 新增 tool/script 字符串操作名分支 +
            _looks_like_cmd 保守递归，原仅识别内层 action/op/操作 键导致
            no_action_detected）
    v3.4.1  平台 planner 协议适配：无 action/标记时读 parameters/planner_parameters
            （平台已从用户消息提取 direction/grade/facts，2026-09-16 平台对话实测
            确认该形态导致 no_action_detected、模型拿不到赛事/优势数据）；
            direction 存在 → 自动按 full_packages 单方向取全量数据包返回；
            facts 只做确定映射（physical_condition→心肺、prior_experience→
            已学时长/零基础、grade），俗称方向名经 resolve 归一为标准名
    v3.4.0  references 由六大类库（每库五类数据）拆为 30 个「大类×数据类型」
            细粒度库（references_{大类简称}_{类型}.md，最短 10 行/最长 96 行）；
            脚本懒加载从大类库粒度细化为（大类, 类型）单文件粒度，赛事全局
            检索只载 6 个赛事库；references_01 表六第三列改存大类简称
    v3.2.0  references 重构为按大类分库（02~07 六库，每库一个大类的全部五类数据），
            脚本改按 references_01 表六路由懒加载：查询只解析所属大类库；
            references_01 新增表六「方向→大类归属」+ 表七「常见叫法对照」
            （自原 05 §一迁入）；原 02~06 五个按数据类型的库废止
    v3.1.1  full_packages 池自动补全：tags 提供时池内未传入的方向自动补入候选
            （顶层 grade 作缺省年级），标签覆盖保底不再依赖模型传全 items
    v3.1.0  full_packages 收敛改两阶段（①主兴趣首位+每标签至少 1 代表保底 ②剩余名额
            按可行度→池序补足，上限 3）；落选项区分 tag_covered/rank
    v3.0.0  按新 Case 框架（A-D × F/G）重构；full_packages 收敛引擎；必要条件判定
            改由条件库驱动；起步表五科按路线过滤；赛事库合并
"""
import json
import os
import random
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REF_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "references")

DASH = "—"
FIVE_SUBJECTS = ["数学特长", "信息学特长", "物理特长", "化学特长", "生物特长"]
WINDOW_DIRS = {"舞蹈特长", "武术特长", "水上运动特长"}  # 窗口敏感方向
FLEX_DIRS = {"舞蹈特长", "武术特长"}  # 窗口判定含柔韧项

# 全局 facts 白名单：仅孩子/家庭层面的通用属性可从 full_packages 顶层 facts
# 打底到全部候选（含池自动补全方向）。零基础/已学时长/起学年级/持续训练/
# 柔韧达标/臂展达标 是方向语境键（游泳零基础≠美术零基础），只认 item 级
# 传入，顶层带了也不打底——防止跨流程承接时污染其他方向的窗口判定。
GLOBAL_FACT_KEYS = {"grade", "年级", "城市", "教育投入", "心肺", "色觉", "听力",
                    "成绩", "家庭规划", "读写耐力", "共情理解", "读写致用"}

STANDARD_DIRECTIONS = [
    "数学特长", "信息学特长", "物理特长", "化学特长", "生物特长",
    "机器人特长", "科学创新特长", "科技体育特长",
    "美术特长", "音乐特长", "舞蹈特长", "传媒特长",
    "田径特长", "球类运动特长", "水上运动特长", "武术特长",
    "小语种能力培养", "阅读素养",
    "思辨能力", "商业洞察", "社会认知",
]

# 大类库起步节的短名 → 标准方向名
SHORT2STD = {
    "数学": "数学特长", "信息学": "信息学特长", "物理": "物理特长",
    "物理学": "物理特长", "化学": "化学特长", "生物": "生物特长", "机器人": "机器人特长",
    "科学创新": "科学创新特长", "科技体育": "科技体育特长", "美术": "美术特长",
    "音乐": "音乐特长", "舞蹈": "舞蹈特长", "传媒": "传媒特长",
    "田径": "田径特长", "球类": "球类运动特长", "武术": "武术特长",
    "水上运动": "水上运动特长", "小语种": "小语种能力培养", "思辨": "思辨能力",
    "商业洞察": "商业洞察", "社会认知": "社会认知", "阅读素养": "阅读素养",
}

GRADE_LEVEL = {
    "一年级": 1, "二年级": 2, "三年级": 3, "四年级": 4, "五年级": 5, "六年级": 6,
    "初一": 7, "初二": 8, "初三": 9,
}
GRADE_NORM = {
    "小学一年级": "一年级", "小学二年级": "二年级", "小学三年级": "三年级",
    "小学四年级": "四年级", "小学五年级": "五年级", "小学六年级": "六年级",
    "初中一年级": "初一", "初中二年级": "初二", "初中三年级": "初三",
    "七年级": "初一", "八年级": "初二", "九年级": "初三",
}

VERDICT_RANK = {"可行": 0, "有条件可行": 1, "待信息": 2, "路线受限": 3}

# 初步判断白名单：缺项全部属于"可后续补充确认"的非核心项、且家长已实际答过
# 其余条件时，verdict 不停在待信息，升级为有条件可行并给出初步判断口径
# （2026-09-18 用户口径：仅臂展没给到也应给"初步判断合适，但还需要…"）。
# 核心缺项（安全类条件/起学窗口/竞赛路线年级开关/成绩位置——数理库明写
# 成绩未采集"不许判定路线、不许写结论"）仍按待信息；年级开关由 grade 满足
# 不算"已答事实"，防止空事实仅凭年级就升级。
SOFT_MISSING = {"臂展", "教育投入", "家庭规划",
                "读写耐力", "共情理解", "读写致用"}


# ---------------------------------------------------------------- 基础工具

def _read(name):
    with open(os.path.join(REF_DIR, name), encoding="utf-8") as f:
        return f.read()


def _tables(md):
    """提取 markdown 里所有表格，每个返回 (header列, rows=[[单元格], …])。"""
    blocks, cur = [], []
    for line in md.splitlines():
        if line.lstrip().startswith("|"):
            cur.append(line)
        else:
            if cur:
                blocks.append(cur)
                cur = []
    if cur:
        blocks.append(cur)
    out = []
    for blk in blocks:
        if len(blk) < 2:
            continue
        header = [c.strip() for c in blk[0].strip().strip("|").split("|")]
        rows = []
        for ln in blk[2:]:
            rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
        out.append((header, rows))
    return out


def _cell(row, i):
    return row[i] if i < len(row) else ""


def _norm_grade(g):
    g = (str(g) if g is not None else "").strip()
    if not g or g == DASH:
        return None
    if g in GRADE_LEVEL:
        return g
    return GRADE_NORM.get(g)


def _grade_level(g):
    gg = _norm_grade(g)
    return GRADE_LEVEL.get(gg) if gg else None


_UNKNOWN_WORDS = ("不清楚", "无法确定", "不确定", "不知道", "说不好", "说不清",
                 "说不准", "记不清", "记不得", "不记得", "没注意", "没了解",
                 "未测", "没测", "没测过", "没量过", "没量")


def _unknown(v):
    """家长明确回答"不清楚/现在无法确定"等 = 已答未知：不再追问、只按待信息呈现。

    短句长度上限防误伤长句（长句含信息，不归未知口径）；布尔/空值不是已答未知。
    """
    if v is None or isinstance(v, bool):
        return False
    s = str(v).strip()
    return bool(s) and len(s) <= 12 and any(w in s for w in _UNKNOWN_WORDS)


def _truthy(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip()
    if s in ("", DASH, "未知", "不清楚"):
        return None
    if s in ("是", "true", "True", "在学", "持续", "达标", "正常", "有", "接受", "可以"):
        return True
    if s in ("否", "false", "False", "停了", "没学", "不达标", "不接受", "不可以", "无"):
        return False
    return None


def _years(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def _norm_score(v):
    if not v:
        return None
    s = str(v)
    if any(k in s for k in ("优", "拔尖", "前5", "前 5", "前列", "顶尖")):
        return "优秀"
    if any(k in s for k in ("中", "下", "一般", "普通")):
        return "中等"
    return s if s in ("优秀", "拔尖", "中等", "偏下") else None


def _ask_split(cell):
    parts = re.split(r"<br\s*/?>|\n", cell)
    return [p.strip() for p in parts if p.strip() and p.strip() != DASH]


def _std(name):
    """把任意叫法对齐到标准方向名；对不上返回 None。"""
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None
    if s in _LOOKUP:
        return _LOOKUP[s]
    t = s.replace("特长", "").replace("能力培养", "").replace("素养", "")
    if t and t in _LOOKUP:
        return _LOOKUP[t]
    for k, v in _LOOKUP.items():
        if k in s:
            return v
    return None


# ---------------------------------------------------------------- 解析层
# 启动只解析 references_01（含表六「方向→大类」路由表、表七俗称对照）；
# 其余 30 个细粒度库懒加载：查某方向某类数据，只解析对应的那一个小文件
# （references_{大类简称}_{数据类型}.md，键 = (大类简称, 数据类型)）。

DTYPES = ("必要条件", "简要说明", "赛事", "优势", "起步")

TAG_LABELS = []      # [{seq, code, text, type}]
CLUES = {}           # {P1: 线索串}
AMBIGUITY = []       # [{线索, 问题, 归此, 归彼}]
TAG_POOL = {}        # {P1: [方向…按表内顺序]}
DIR_EXAMPLES = {}    # {方向: [俗称…]}
CATEGORY_FILES = {}  # {方向: 大类简称}（references_01 表六路由）
_CAT_CACHE = {}      # {(大类简称, 数据类型): {…该类型解析结果}}

# 额外常用别名（references_01 表四括号示例 / 表七对照之外的通行叫法）
_EXTRA_ALIAS = {
    "编程": "信息学特长", "信奥": "信息学特长", "信息学奥赛": "信息学特长",
    "奥数": "数学特长", "数学思维": "数学特长", "画画": "美术特长",
    "钢琴": "音乐特长", "小提琴": "音乐特长", "跑步": "田径特长",
    "篮球": "球类运动特长", "足球": "球类运动特长", "排球": "球类运动特长",
    "羽毛球": "球类运动特长", "乒乓球": "球类运动特长", "网球": "球类运动特长",
    "飞行员": "科技体育特长", "航模": "科技体育特长", "无人机": "科技体育特长",
    "科创": "科学创新特长", "乐高": "机器人特长",
}


def _hkw(header, kw):
    """表头子串匹配：列名带括号注解（如「标签文案（展示时逐字原样输出）」）也能命中。"""
    return any(kw in h for h in header)


def _parse_pool_cell(cell):
    """『方向（含俗称、俗称）、方向、…』→ [(方向, [俗称…]), …]"""
    out, i, s = [], 0, cell
    while i < len(s):
        if s[i] in "、，, ":
            i += 1
            continue
        j = i
        while j < len(s) and s[j] not in "（、":
            j += 1
        name = s[i:j].strip()
        exs = []
        if j < len(s) and s[j] == "（":
            k = s.find("）", j)
            inner = s[j + 1:k]
            if inner.startswith("含"):
                inner = inner[1:]
            exs = [e.strip() for e in re.split(r"[、，]", inner) if e.strip()]
            j = k + 1
        if name:
            out.append((name, exs))
        i = j
    return out


def _load_registry():
    """启动时解析 references_01：标签 / 线索 / 判别 / 方向池 / 大类路由 / 俗称对照。"""
    global TAG_LABELS, CLUES, AMBIGUITY, TAG_POOL, DIR_EXAMPLES, CATEGORY_FILES, _LOOKUP
    md1 = _read("references_01_特质标签与方向映射.md")
    lookup = {}
    for header, rows in _tables(md1):
        h0 = header[0] if header else ""
        if h0 == "编号" and _hkw(header, "标签文案"):
            for r in rows:
                TAG_LABELS.append({
                    "code": _cell(r, 0), "seq": _cell(r, 1),
                    "text": _cell(r, 2), "type": _cell(r, 3),
                })
        elif h0 == "编号" and _hkw(header, "常见行为线索"):
            for r in rows:
                CLUES[_cell(r, 0)] = _cell(r, 1)
        elif h0 == "歧义线索":
            for r in rows:
                AMBIGUITY.append({
                    "线索": _cell(r, 0), "问题": _cell(r, 1),
                    "归此": _cell(r, 2), "归彼": _cell(r, 3),
                })
        elif h0 == "编号" and _hkw(header, "对应二级方向"):
            for r in rows:
                code = _cell(r, 0)
                pool, examples = [], {}
                for name, exs in _parse_pool_cell(_cell(r, 2)):
                    if name in STANDARD_DIRECTIONS:
                        pool.append(name)
                        if exs:
                            examples[name] = exs
                TAG_POOL[code] = pool
                DIR_EXAMPLES.update(examples)
        elif h0 == "方向" and _hkw(header, "大类简称"):
            for r in rows:
                if _cell(r, 0) and _cell(r, 2):
                    CATEGORY_FILES[_cell(r, 0)] = _cell(r, 2)
        elif h0 == "常见叫法":
            for r in rows:
                if _cell(r, 0):
                    lookup[_cell(r, 0)] = _cell(r, 1)

    # 归一化查询表：标准名 + 短名 + 俗称对照 + 俗称示例 + 额外别名
    _LOOKUP = {}
    for d in STANDARD_DIRECTIONS:
        _LOOKUP[d] = d
    for short, std in SHORT2STD.items():
        _LOOKUP[short] = std
    _LOOKUP.update(lookup)
    for std, exs in DIR_EXAMPLES.items():
        for e in exs:
            _LOOKUP[e] = std
    _LOOKUP.update(_EXTRA_ALIAS)


def _parse_conditions(md):
    """必要条件库：{方向: [{项, 口径, 问句[], 不满足}]} + 窗口口径表。"""
    data = {"conditions": {}, "window_combo": {}}
    for header, rows in _tables(md):
        if not header:
            continue
        h0 = header[0]
        if h0 == "方向" and _hkw(header, "必要条件项"):
            for r in rows:
                d = _cell(r, 0)
                data["conditions"].setdefault(d, []).append({
                    "项": _cell(r, 1), "口径": _cell(r, 2),
                    "问句": _ask_split(_cell(r, 3)), "不满足": _cell(r, 4),
                })
        elif h0 == "起学窗口" and _hkw(header, "转化窗口"):
            for r in rows:
                data["window_combo"][(_cell(r, 0), _cell(r, 1))] = _cell(r, 2)
    return data


def _parse_briefs(md):
    """简要说明库：{方向: 一句话说明}。"""
    data = {"briefs": {}}
    for header, rows in _tables(md):
        if header and header[0] == "方向" and _hkw(header, "简要说明"):
            for r in rows:
                data["briefs"][_cell(r, 0)] = _cell(r, 1)
    return data


def _parse_events(md):
    """赛事库：{方向: [{赛事行}]} + 本库全部赛事行索引。"""
    data = {"events": {}, "event_index": []}
    for header, rows in _tables(md):
        if header and header[0] == "方向" and _hkw(header, "赛事全称"):
            for r in rows:
                d = _cell(r, 0)
                ev = {
                    "方向": d, "全称": _cell(r, 1),
                    "简称": [s.strip() for s in _cell(r, 2).replace("，", "、").split("、")
                             if s.strip() and s.strip() != DASH],
                    "类别": _cell(r, 3), "主办": _cell(r, 4),
                    "对象": _cell(r, 5), "介绍": _cell(r, 6), "官网": _cell(r, 7),
                }
                data["event_index"].append(ev)
                if d != DASH:
                    data["events"].setdefault(d, []).append(ev)
    return data


def _parse_advantages(md):
    """优势库：{方向: [{维度, 内容}]}。"""
    data = {"advantages": {}}
    for header, rows in _tables(md):
        if header and header[0] == "方向" and _hkw(header, "优势维度"):
            for r in rows:
                data["advantages"].setdefault(_cell(r, 0), []).append(
                    {"维度": _cell(r, 1), "内容": _cell(r, 2)})
    return data


def _parse_steps(md):
    """起步库：### 方向 → #### 年级 → 编号条目（五科条目带路线前缀）。"""
    data = {"steps": {}}
    in_steps = False
    cur_dir, cur_grade = None, None
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_steps = "起步建议" in s
            cur_dir, cur_grade = None, None
            continue
        if not in_steps:
            continue
        if s.startswith("### ") and not s.startswith("#### "):
            cur_dir = SHORT2STD.get(s[4:].strip())
            if cur_dir and cur_dir not in data["steps"]:
                data["steps"][cur_dir] = {}
            cur_grade = None
        elif s.startswith("#### ") and cur_dir:
            g = _norm_grade(s[5:].strip())
            if g:
                cur_grade = g
                data["steps"][cur_dir].setdefault(g, [])
        elif cur_dir and cur_grade and re.match(r"^\d+[\.、]", s):
            text = re.sub(r"^\d+[\.、]\s*", "", s).strip()
            m = re.match(r"^(竞赛路线|素养路线|中考路线)[:：]", text)
            data["steps"][cur_dir][cur_grade].append(
                {"step": text, "route": m.group(1) if m else None})
    return data


_PARSERS = {
    "必要条件": _parse_conditions, "简要说明": _parse_briefs,
    "赛事": _parse_events, "优势": _parse_advantages, "起步": _parse_steps,
}


def _dtype_file(cat, dtype):
    return "references_{}_{}.md".format(cat, dtype)


def _dtype_data(direction, dtype):
    """方向 + 数据类型 → 所属大类该小文件的数据（懒加载 + 缓存）；库外方向返回 None。"""
    if not direction:
        return None
    cat = CATEGORY_FILES.get(direction)
    if cat is None:
        return None
    key = (cat, dtype)
    if key not in _CAT_CACHE:
        _CAT_CACHE[key] = _PARSERS[dtype](_read(_dtype_file(cat, dtype)))
    return _CAT_CACHE[key]


def _ensure_all_events():
    """赛事名全局检索用：加载全部六大赛事库（只载赛事，不载其他类型）。"""
    for cat in sorted(set(CATEGORY_FILES.values())):
        key = (cat, "赛事")
        if key not in _CAT_CACHE:
            _CAT_CACHE[key] = _parse_events(_read(_dtype_file(cat, "赛事")))


def _brief_of(name):
    d = _dtype_data(name, "简要说明")
    return d["briefs"].get(name) if d else None


def _events_of(name):
    d = _dtype_data(name, "赛事")
    return d["events"].get(name, []) if d else []


def _advantages_of(name):
    d = _dtype_data(name, "优势")
    return d["advantages"].get(name) if d else None


def _steps_of(name):
    d = _dtype_data(name, "起步")
    return d["steps"].get(name, {}) if d else {}


_load_registry()


# ---------------------------------------------------------------- 查询操作

def op_tags(payload):
    return {
        "labels": TAG_LABELS,
        "clues": CLUES,
        "ambiguity": AMBIGUITY,
        "展示指引": ("首次展示（本轮之前没展示过这 6 条）：逐条原样展示 6 个标签"
                    "后，末尾必须补一句兜底邀约，给家长描述孩子特质的入口，措辞"
                    "参考：\"如果这几点都不太像，也没关系——您直接说说孩子平时的"
                    "情况，我来帮您对照判断\"（可按语气微调，不得省略）。已展示过"
                    "（家长本轮是在回复标签或描述孩子情况）：禁止重发 6 条，改用"
                    "clues 对家长描述做归类确认——\"孩子{≤12字概括}这一点，最接"
                    "近下面这条：{序号}{标签原文}，是这个意思吗？\"归类不出就把"
                    "最接近的两条列给家长选。"),
    }


def _norm_tags(tags):
    out = []
    seq_map = {"①": "P1", "②": "P2", "③": "P3", "④": "P4", "⑤": "P5", "⑥": "P6"}
    for t in tags or []:
        s = str(t).strip().upper()
        code = None
        if s in seq_map:
            code = seq_map[s]
        elif re.fullmatch(r"P[1-6]", s):
            code = s
        elif re.fullmatch(r"[1-6]", s):
            code = "P" + s
        if code and code in TAG_POOL and code not in out:
            out.append(code)
    return out


def op_feature_pool(payload):
    tags = _norm_tags(payload.get("tags"))
    per_tag = {t: TAG_POOL[t] for t in tags}
    pool = []
    for t in tags:
        for d in TAG_POOL[t]:
            if d not in pool:
                pool.append(d)
    return {"tags": tags, "per_tag": per_tag, "pool": pool}


def op_resolve_direction(payload):
    text = (payload.get("text") or payload.get("name") or "").strip()
    if not text:
        return {"found": False, "standard_name": None, "in_registry": False}
    name = _std(text)
    if name:
        return {"found": True, "standard_name": name, "in_registry": True}
    return {
        "found": False, "standard_name": None, "in_registry": False,
        "examples": {d: exs for d, exs in DIR_EXAMPLES.items() if exs},
    }


def op_direction_brief(payload):
    name = _std(payload.get("name"))
    brief = _brief_of(name) if name else None
    if not name or brief is None:
        return {"name": name or payload.get("name"), "found": False, "brief": None}
    return {"name": name, "found": True, "brief": brief}


def _adv_payload(adv):
    parts = []
    summary = None
    for r in adv:
        if r["维度"] == "总述":
            summary = r["内容"]
            parts.append(r["内容"])
        else:
            parts.append(r["维度"] + "：" + r["内容"].replace("<br>", "\n"))
    return {
        "found": True,
        "summary": summary,
        "dimensions": [r["维度"] for r in adv if r["维度"] != "总述"],
        "block": "\n".join(parts),
    }


def op_direction_advantage(payload):
    name = _std(payload.get("name"))
    adv = _advantages_of(name) if name else None
    if not name or not adv:
        return {"name": name or payload.get("name"), "found": False,
                "summary": None, "dimensions": [], "block": None}
    out = _adv_payload(adv)
    out["name"] = name
    return out


def _competitions_payload(name):
    names = [e["全称"] for e in _events_of(name)]
    display = list(names) if len(names) <= 3 else random.sample(names, 3)
    return {"found": bool(names), "count": len(names), "names": names, "display": display}


def op_direction_competitions(payload):
    name = _std(payload.get("name"))
    if not name:
        return {"name": payload.get("name"), "found": False, "count": 0,
                "names": [], "display": []}
    out = _competitions_payload(name)
    out["name"] = name
    return out


def op_competition_detail(payload):
    text = (payload.get("text") or "").strip()
    if not text:
        return {"matched_fullname": None, "has_detail": False,
                "ambiguous_candidates": [], "detail": None}
    _ensure_all_events()  # 赛事名检索不知道方向 → 只加载全部六大赛事库
    index = [ev for data in _CAT_CACHE.values() if "event_index" in data
             for ev in data["event_index"]]
    hits = []
    for ev in index:
        if text == ev["全称"] or text in ev["简称"]:
            hits.append(ev)
    if not hits:
        return {"matched_fullname": None, "has_detail": False,
                "ambiguous_candidates": [], "detail": None}
    if len(hits) > 1:
        return {"matched_fullname": None, "has_detail": False,
                "ambiguous_candidates": [h["全称"] for h in hits], "detail": None}
    ev = hits[0]
    detail = {
        "方向": ev["方向"], "类别": ev["类别"], "主办单位": ev["主办"],
        "参赛对象": ev["对象"], "赛事介绍": ev["介绍"], "官网": ev["官网"],
    }
    has = any(v != DASH for k, v in detail.items() if k != "方向")
    return {"matched_fullname": ev["全称"], "has_detail": has,
            "ambiguous_candidates": [], "detail": detail}


def _steps_payload(name, grade, route=None):
    grade_n = _norm_grade(grade)
    items = _steps_of(name).get(grade_n, []) if name else []
    if route and name in FIVE_SUBJECTS:
        items = [it for it in items if it["route"] in (None, route)]
    return {"found": bool(items), "grade": grade_n,
            "steps": [{"step": it["step"], "route": it["route"]} for it in items]}


def op_starting_steps(payload):
    name = _std(payload.get("name") or payload.get("direction"))
    grade = payload.get("grade", payload.get("年级"))
    route = payload.get("route")
    out = _steps_payload(name, grade, route)
    out["name"] = name
    return out


# ---------------------------------------------------------------- 判定引擎

def _miss(row, missing, ask, asked, unknown=False):
    """缺项登记；unknown=True 表示家长已明确回答"不清楚"类——只按待信息呈现，问句不再进 ask。"""
    missing.append({"项": row["项"]})
    if unknown:
        return
    for q in row["问句"]:
        if q not in asked:
            asked.add(q)
            ask.append(q)


def _window_check(direction, grade, facts, row, combo):
    """返回 (状态, 口径)：状态 ∈ ok / 受限 / 有条件 / missing。"""
    bad = row["不满足"]
    lvl = _grade_level(grade)
    zero = _truthy(facts.get("零基础"))
    起学lvl = _grade_level(facts.get("起学年级"))
    if 起学lvl is None:
        时长 = _years(facts.get("已学时长"))
        if 时长 and lvl:
            推算 = lvl - 时长
            if 推算 >= 1:
                起学lvl = 推算
    if zero:
        # 库口径"零基础（初中起学）→ 窗口必不满足"只限初中在读（年级未知按严）；
        # 小学在读零基础 = 现在起步、起学仍在窗口内，不再追问起学/持续/柔韧
        if lvl is None or lvl >= 7:
            return ("受限", bad)
        return ("ok", None)
    if 起学lvl is not None and 起学lvl >= 7:
        return ("受限", bad)
    时长 = _years(facts.get("已学时长"))
    持续 = _truthy(facts.get("持续训练至今"))
    柔韧 = _truthy(facts.get("柔韧达标")) if direction in FLEX_DIRS else True
    if 时长 is not None and 持续 and 时长 >= 4:
        start_ok = True
    elif 起学lvl is not None and 起学lvl <= 6:
        if 持续 is None or (direction in FLEX_DIRS and 柔韧 is None):
            return ("missing", row)
        start_ok = bool(持续 and 柔韧)
    else:
        return ("missing", row)
    if not start_ok:
        return ("受限", bad)
    if lvl is not None and lvl >= 9:
        text = combo.get(("满足", "不满足"), "")
        return ("有条件", text or "方向与专业路线均不否定，初中阶段的特长生升学仍可探索，素养路线保留")
    return ("ok", None)


def hard_gate_check(payload):
    direction = _std(payload.get("direction") or payload.get("name"))
    if not direction:
        return {"error": "未知方向：" + str(payload.get("direction"))}
    data = _dtype_data(direction, "必要条件")
    if data is None:
        return {"error": "方向未归属大类库：" + direction}
    facts = payload.get("facts") or {}
    grade = payload.get("grade") or payload.get("年级") or facts.get("grade") or facts.get("年级")
    lvl = _grade_level(grade)
    rows = data["conditions"].get(direction, [])
    combo = data["window_combo"]
    limits, missing, ask = [], [], []
    asked = set()
    answered = 0  # 家长实际提供过有效事实的条件行数（grade 满足的年级开关不计）
    route_limited = False
    cond_feasible = False
    window_state = None

    for row in rows:
        item = row["项"]
        if item.startswith("安全类条件"):
            v = str(facts.get("心肺") or "")
            if not v or v in (DASH, "未知"):
                _miss(row, missing, ask, asked)
            elif _unknown(v):
                _miss(row, missing, ask, asked, unknown=True)
            else:
                answered += 1
                if v not in ("无碍", "正常", "健康", "没问题"):
                    route_limited = True
                    limits.append({"项": item, "口径": row["不满足"]})
        elif item == "起学窗口":
            state, text = _window_check(direction, grade, facts, row, combo)
            window_state = state
            if state == "missing":
                if any(_unknown(facts.get(k)) for k in
                       ("起学年级", "已学时长", "持续训练至今", "柔韧达标", "零基础")):
                    _miss(row, missing, ask, asked, unknown=True)
                else:
                    _miss(row, missing, ask, asked)
            elif state == "受限":
                route_limited = True
                limits.append({"项": item, "口径": text})
            else:
                answered += 1
                if state == "有条件":
                    cond_feasible = True
                    limits.append({"项": "转化窗口", "口径": text})
        elif item == "臂展":
            if window_state == "受限":
                continue  # 窗口已过不再追问臂展（条件库口径）
            v = _truthy(facts.get("臂展达标"))
            if _unknown(facts.get("臂展达标")):
                _miss(row, missing, ask, asked, unknown=True)
            elif v is None:
                _miss(row, missing, ask, asked)
            else:
                answered += 1
                if v is False:
                    route_limited = True
                    limits.append({"项": item, "口径": row["不满足"]})
        elif item == "色觉":
            v = str(facts.get("色觉") or "")
            if not v or v in (DASH, "未知"):
                _miss(row, missing, ask, asked)
            elif _unknown(v):
                _miss(row, missing, ask, asked, unknown=True)
            else:
                answered += 1
                if v not in ("正常", "无色盲", "没问题"):
                    route_limited = True
                    limits.append({"项": item, "口径": row["不满足"]})
        elif item == "听力":
            v = str(facts.get("听力") or "")
            if not v or v in (DASH, "未知"):
                _miss(row, missing, ask, asked)
            elif _unknown(v):
                _miss(row, missing, ask, asked, unknown=True)
            else:
                answered += 1
                if v not in ("正常", "没问题"):
                    route_limited = True
                    limits.append({"项": item, "口径": row["不满足"]})
        elif item == "家庭规划":
            v = _truthy(facts.get("家庭规划"))
            if _unknown(facts.get("家庭规划")):
                _miss(row, missing, ask, asked, unknown=True)
            elif v is None:
                _miss(row, missing, ask, asked)
            else:
                answered += 1
                if v is False:
                    route_limited = True
                    limits.append({"项": item, "口径": row["不满足"]})
        elif item in ("读写耐力", "共情理解", "读写致用"):
            # 阅读素养软性素养项（条件库口径）：不满足只降可行度、方向保留
            v = _truthy(facts.get(item))
            if _unknown(facts.get(item)):
                _miss(row, missing, ask, asked, unknown=True)
            elif v is None:
                _miss(row, missing, ask, asked)
            else:
                answered += 1
                if v is False:
                    cond_feasible = True
                    limits.append({"项": item, "口径": row["不满足"]})
        elif item == "竞赛路线年级开关":
            if lvl is None:
                _miss(row, missing, ask, asked)
            elif lvl >= 9:
                route_limited = True
                limits.append({"项": item, "口径": row["不满足"]})
        elif item == "成绩位置":
            if lvl is None or lvl >= 9 or lvl < 7:
                continue  # 初三竞赛已关不问成绩；小学不在判定范围
            v = _norm_score(facts.get("成绩"))
            if _unknown(facts.get("成绩")):
                _miss(row, missing, ask, asked, unknown=True)
            elif v is None:
                _miss(row, missing, ask, asked)
            else:
                answered += 1
                if v != "优秀":
                    route_limited = True
                    limits.append({"项": item, "口径": row["不满足"]})
        elif item == "教育投入":
            v = str(facts.get("教育投入") or "")
            if not v or v in (DASH, "未知"):
                _miss(row, missing, ask, asked)
            elif _unknown(v):
                _miss(row, missing, ask, asked, unknown=True)
            else:
                answered += 1
                if any(k in v for k in ("平替", "紧张", "不足", "有限", "偏低")):
                    cond_feasible = True
                    limits.append({"项": item, "口径": row["不满足"]})

    five_route = None
    if direction in FIVE_SUBJECTS:
        if lvl is not None and lvl >= 9:
            five_route = "素养路线"
        elif lvl is not None and 7 <= lvl < 9:
            v = _norm_score(facts.get("成绩"))
            if v == "优秀":
                five_route = "竞赛路线"
            elif v:
                five_route = "素养路线"

    if route_limited:
        verdict = "路线受限"
    elif cond_feasible:
        verdict = "有条件可行"
    elif missing:
        if (answered > 0
                and all(m["项"] in SOFT_MISSING for m in missing)):
            verdict = "有条件可行"
            limits.append({
                "项": "初步判断",
                "口径": "其余条件满足，%s待确认——按已知信息初步判断本方向可行，"
                        "该项补充后再把结论落准" % "、".join(
                            m["项"] for m in missing)})
        else:
            verdict = "待信息"
    else:
        verdict = "可行"

    return {
        "direction": direction,
        "grade": _norm_grade(grade),
        "verdict": verdict,
        "five_subject_route": five_route,
        "limits": limits,
        "missing_required": missing,
        "ask": ask,
    }


# ---------------------------------------------------------------- 收敛引擎

def _interest_primary(records):
    for r in records:
        if r["is_interest"]:
            return r["name"]
    return None


def full_packages(payload):
    """批量定稿 + 收敛（两阶段，上限 3）：
    ①保底——主兴趣（或家长带来的方向）首位 + 每个已确认标签至少 1 个代表
      （按 可行度→池序 取该标签最优）；②剩余名额按 可行度→池序→传入顺序 补足。
    池内未传入的方向自动补入候选（tags 驱动，顶层 grade 作缺省年级），
    标签覆盖不依赖模型传全池。库外兴趣不进 items（由模型按附段处理），
    归库兴趣以 is_interest 标记且全场只认第一个。
    顶层 facts（全局通用项，白名单键）打底到全部候选——跨流程承接
    （G/F 采集过的城市/教育投入/心肺等）不再被池补全方向重复追问；
    item 级同名键覆盖顶层；方向语境键（零基础/已学时长等）不打底。
    涉及方向分属多个大类时，只加载涉及的那几个大类库。"""
    if isinstance(payload, list):
        payload = {"items": payload}
    tags = _norm_tags(payload.get("tags"))
    items = payload.get("items") or payload.get("directions") or []
    raw_gf = payload.get("facts")
    gfacts = {k: v for k, v in raw_gf.items() if k in GLOBAL_FACT_KEYS} \
        if isinstance(raw_gf, dict) else {}

    fp = op_feature_pool({"tags": tags})
    pool = fp["pool"]
    pool_index = {d: i for i, d in enumerate(pool)}

    records, seen = [], {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _std(it.get("name") or it.get("direction"))
        if not name:
            continue
        facts = {**gfacts, **dict(it.get("facts") or {})}
        if name in seen:
            seen[name]["facts"].update(facts)
            if it.get("is_interest"):
                seen[name]["is_interest"] = True
            continue
        rec = {
            "name": name,
            "grade": it.get("grade") or it.get("年级") or facts.get("grade") or facts.get("年级"),
            "facts": facts,
            "is_interest": bool(it.get("is_interest")),
            "info": bool(it.get("info")),
            "order": len(records),
        }
        seen[name] = rec
        records.append(rec)

    # 池自动补全：tags 提供时，池内未传入的方向一律补入候选（吃全局 facts
    # 打底），收敛的标签覆盖保底不再依赖模型是否把池传全；顶层 grade 作缺省年级。
    base_grade = payload.get("grade") or payload.get("年级") or gfacts.get("grade") or gfacts.get("年级")
    for name in pool:
        if name in seen:
            continue
        rec = {"name": name, "grade": base_grade, "facts": dict(gfacts),
               "is_interest": False, "info": False, "order": len(records)}
        seen[name] = rec
        records.append(rec)
    if base_grade:
        for r in records:
            if not r["grade"]:
                r["grade"] = base_grade

    primary = _interest_primary(records)

    for r in records:
        r["gate"] = None if r["info"] else hard_gate_check(
            {"direction": r["name"], "grade": r["grade"], "facts": r["facts"]})

    def gate_rank(r):
        return VERDICT_RANK.get(r["gate"]["verdict"], 2) if r["gate"] else 0

    # 两阶段收敛（上限 3）：①保底——主兴趣首位 + 每个已确认标签至少 1 个代表
    # （按 可行度→池序 取该标签最优）；②剩余名额按 可行度→池序→传入顺序 补足。
    per_tag = fp.get("per_tag") or {}
    tag_of = {}
    for tag, dirs in per_tag.items():
        for n in dirs:
            tag_of[n] = tag

    by_name = {r["name"]: r for r in records}
    selected, selected_names = [], set()

    def take(r):
        if r["name"] not in selected_names:
            selected_names.add(r["name"])
            selected.append(r)

    if primary and primary in by_name:
        take(by_name[primary])
    for tag, dirs in per_tag.items():
        if any(n in selected_names for n in dirs):
            continue
        cand = [by_name[n] for n in dirs if n in by_name]
        if not cand:
            continue
        take(min(cand, key=lambda r: (gate_rank(r), pool_index.get(r["name"], len(pool)), r["order"])))
    rest = sorted((r for r in records if r["name"] not in selected_names),
                  key=lambda r: (gate_rank(r), pool_index.get(r["name"], len(pool)), r["order"]))
    for r in rest:
        if len(selected) < 3:
            take(r)
    kept = selected
    dropped = []
    for r in rest:
        if r["name"] in selected_names:
            continue
        tag = tag_of.get(r["name"])
        covered = bool(tag) and any(n in selected_names for n in per_tag.get(tag, []))
        dropped.append({
            "name": r["name"],
            "原因": "该标签已有代表入选" if covered else "可行度/池序落选",
            "reason": "tag_covered" if covered else "rank",
        })

    out_kept = []
    ask_union, asked = [], set()
    for r in kept:
        pkg = {
            "name": r["name"],
            "is_interest": bool(primary and r["name"] == primary),
            "gate": r["gate"],
            "brief": _brief_of(r["name"]),
            "competitions": _competitions_payload(r["name"]),
        }
        adv = _advantages_of(r["name"])
        pkg["advantage"] = _adv_payload(adv) if adv else {
            "found": False, "summary": None, "dimensions": [], "block": None}
        route = r["gate"].get("five_subject_route") if r["gate"] else None
        pkg["steps"] = _steps_payload(r["name"], r["grade"], route)
        out_kept.append(pkg)
        if r["gate"]:
            for q in r["gate"]["ask"]:
                if q not in asked:
                    asked.add(q)
                    ask_union.append(q)

    # 安全类问句置顶
    ask_union.sort(key=lambda q: 0 if re.search(r"心肺|哮喘|心脏", q) else 1)

    # v3.6.0 两阶段协议（推荐主流程 M5）+ v3.7.0 去开关化：标签池形态下
    # 必要条件未采集齐（ask_union 非空）→ 只返回问句，不返回结论版块
    # （说明/赛事/优势/起步）——模型无结论可提前输出，只能先问；
    # 唯一出口 = ask_union 清空：家长作答（值进 facts）或答"不清楚/未提供/
    # 拒答"（原话进 facts，脚本按已答未知处理、不回填问句）→ 重调自动
    # phase=final 给全版块。v3.6.0 的 force_final 参数开关已被平台实测
    # 滥用（模型一轮就带 force_final:true 解锁跳过提问，2026-09-22
    # 日志实锤）→ v3.7.0 删除：模型想跳过提问只能往 facts 编造值，
    # 日志可审计。单方向形态（G/F 场景、无 tags）不锁：v3.5.3
    # SOFT_MISSING 初步结论口径不变；单方向出参带 notice，判定轮不重贴。
    result = {
        "pool": pool,
        "phase": "collect" if tags and ask_union else "final",
        "kept": out_kept,
        "dropped": dropped,
        "interest": {"name": primary} if primary else None,
        "ask_union": ask_union,
        "has_ask": bool(ask_union),
    }
    if result["phase"] == "collect":
        for pkg in out_kept:
            for blk in ("brief", "competitions", "advantage", "steps"):
                pkg.pop(blk, None)
        result["指令"] = ("必要条件未采集齐：本轮输出三段——先一句大方向定性"
                           "（按标签说孩子偏哪个大类：①数理/②科创/③体育/④艺术/"
                           "⑤国际/⑥人文，多标签并列；只说大类，禁止列具体方向名），"
                           "再一句提问目的（问这些条件是为了帮孩子在这个方向里筛出"
                           "最合适的特长），然后问 ask_union 问句。问句 >3 个时分两轮"
                           "（第一轮孩子的情况：安全类置顶+身体条件+学习经历；"
                           "第二轮家里的情况：城市/投入），≤3 个一轮问完。2.2 盘点"
                           "学过的方向先在 items 带起学/时长/持续；没学过的池方向"
                           "本轮删掉其起学/时长/持续/柔韧/臂展类问句（对没学过的"
                           "孩子是预设性错问），重调时 items 补\"零基础\":\"是\"。"
                           "禁止输出具体方向推荐结论（总览表/优势/赛事/起步/verdict/"
                           "具体方向名）。家长作答后把答案放进 facts 重新调用；家长答"
                           "\"不清楚/没有/不打算提供\"或拒答 → 原话如实写进 facts 重调"
                           "（脚本按已答未知处理，不回填问句），问句清空即自动出终版"
                           "——没有跳过提问的开关，未答项按「待补充」呈现，禁编造。")
    elif not tags:
        result["notice"] = ("判定场景只输出 gate 判定段；说明/赛事/优势/起步块"
                            "仅本场首次展示时输出，已展示过的不重贴（G8-12）")
        if ask_union:
            result["notice"] += ("；ask 里本场已答过的项（对照 G7 台账）："
                                 "删问句不重问，把已答值放进 facts 重调一次取判定")
    return result


OPS = {
    "tags": op_tags,
    "feature_pool": op_feature_pool,
    "resolve_direction": op_resolve_direction,
    "direction_brief": op_direction_brief,
    "direction_advantage": op_direction_advantage,
    "direction_competitions": op_direction_competitions,
    "competition_detail": op_competition_detail,
    "starting_steps": op_starting_steps,
    "hard_gate_check": hard_gate_check,
    "full_packages": full_packages,
}


# ---------------------------------------------------------------- 冒烟测试

def _smoke():
    ok, total = [0], [0]

    def check(name, cond, detail=""):
        total[0] += 1
        ok[0] += 1 if cond else 0
        print(("PASS" if cond else "FAIL") + "  " + name + (("  | " + str(detail)) if (detail and not cond) else ""))

    # 大类路由
    check("路由: 21 方向全部归属大类库", len(CATEGORY_FILES) == 21
          and all(CATEGORY_FILES.get(d) for d in STANDARD_DIRECTIONS),
          {d: CATEGORY_FILES.get(d) for d in STANDARD_DIRECTIONS})
    check("路由: 6 个大类×5 类型细粒度库齐全", len(set(CATEGORY_FILES.values())) == 6
          and all(os.path.exists(os.path.join(REF_DIR, _dtype_file(c, d)))
                  for c in set(CATEGORY_FILES.values()) for d in DTYPES))

    t = op_tags({})
    check("tags: 6 标签", len(t["labels"]) == 6, t["labels"])
    check("tags: 线索/判别", len(t["clues"]) == 6 and len(t["ambiguity"]) >= 5)

    fp = op_feature_pool({"tags": [1, 2]})
    check("feature_pool: P1+P2=8", len(fp["pool"]) == 8, fp["pool"])
    fp1 = op_feature_pool({"tags": ["③"]})
    check("feature_pool: P3=5", len(fp1["pool"]) == 5, fp1["pool"])

    r = op_resolve_direction({"text": "游泳"})
    check("resolve: 游泳→水上", r["standard_name"] == "水上运动特长", r)
    r = op_resolve_direction({"text": "马术"})
    check("resolve: 马术库外+示例", r["found"] is False and "examples" in r, r)
    r = op_resolve_direction({"text": "舞蹈"})
    check("resolve: 舞蹈→归库", r["standard_name"] == "舞蹈特长", r)
    r = op_resolve_direction({"text": "生物学"})
    check("resolve: 表七对照 生物学→生物", r["standard_name"] == "生物特长", r)

    # v3.5.0 取消方向：全库库外化（路由表/方向池/归一查询都不再命中）
    r_en = op_resolve_direction({"text": "英语能力培养"})
    r_zh = op_resolve_direction({"text": "综合能力培养"})
    check("删除: 英语/综合能力培养已出库",
          r_en["found"] is False and r_zh["found"] is False
          and "英语能力培养" not in CATEGORY_FILES
          and "综合能力培养" not in CATEGORY_FILES
          and all("英语能力培养" not in p and "综合能力培养" not in p
                  for p in TAG_POOL.values()), (r_en, r_zh))

    r = op_resolve_direction({"text": "读写素养"})
    check("resolve: 读写素养→阅读素养", r["standard_name"] == "阅读素养", r)

    # 懒加载：查舞蹈说明只解析艺术·简要说明一个小文件
    _CAT_CACHE.clear()
    b = op_direction_brief({"name": "舞蹈特长"})
    check("懒加载: 查舞蹈说明只载艺术·简要说明单文件",
          list(_CAT_CACHE) == [("艺术", "简要说明")], list(_CAT_CACHE))
    check("brief: 舞蹈", b["found"] and "身体语言" in b["brief"], b)

    # 拆分完整性：31 个 reference 文件 = 01 + 6 大类 × 5 类型
    ref_files = sorted(f for f in os.listdir(REF_DIR) if f.endswith(".md"))
    expect_refs = sorted(["references_01_特质标签与方向映射.md"] + [
        "references_%s_%s.md" % (c, d) for c in
        ("数理", "科创", "艺术", "体育", "国际", "人文") for d in DTYPES])
    check("拆分: 31 文件 = 01 + 6大类×5类型", ref_files == expect_refs,
          [f for f in ref_files if f not in expect_refs]
          + [f for f in expect_refs if f not in ref_files])

    a = op_direction_advantage({"name": "数学特长"})
    check("advantage: 数学维度", a["found"] and len(a["dimensions"]) == 3
          and "总述" not in a["dimensions"]
          and a["dimensions"] == ["学业优势", "升学优势", "职业优势"], a["dimensions"])
    check("advantage: block 含升学", "升学优势" in a["block"], a["block"][:80])

    # v3.5.4 多方向总述：summary 字段（有总述=原文首段；无总述方向=null）
    a_sum = op_direction_advantage({"name": "舞蹈特长"})
    check("advantage: summary=总述原文",
          a_sum["summary"] and a_sum["summary"] in a_sum["block"]
          and a_sum["summary"] == a_sum["block"].split("\n")[0], a_sum.get("summary"))
    a_rb = op_direction_advantage({"name": "机器人特长"})
    check("advantage: 无总述方向 summary=null（禁自编总述）",
          a_rb["found"] and a_rb["summary"] is None
          and a_rb["dimensions"], a_rb.get("summary"))

    c = op_direction_competitions({"name": "数学特长"})
    check("competitions: 数学 4 条（v3.6.0 库更新删小学向赛事）",
          c["count"] == 4 and len(c["display"]) == 3, c)
    c2 = op_direction_competitions({"name": "武术特长"})
    check("competitions: 武术 1 条全显", c2["count"] == 1 and len(c2["display"]) == 1, c2)
    c3 = op_direction_competitions({"name": "阅读素养"})
    check("competitions: 阅读素养 8 条（人文库）", c3["count"] == 8, c3)

    d = op_competition_detail({"text": "高联"})
    check("detail: 简称命中",
          d["matched_fullname"] == "全国中学生数学奥林匹克竞赛（高联）", d)
    d2 = op_competition_detail({"text": "不存在的比赛"})
    check("detail: 未命中", d2["matched_fullname"] is None)
    d3 = op_competition_detail({"text": "华杯赛"})
    check("detail: 已删赛事不再命中（库更新防回归）",
          d3["matched_fullname"] is None, d3)

    s = op_starting_steps({"name": "数学特长", "grade": "初一", "route": "素养路线"})
    routes = {it["route"] for it in s["steps"]}
    check("steps: 初一素养过滤", "竞赛路线" not in routes and None in routes, s["steps"])
    s2 = op_starting_steps({"name": "物理特长", "grade": "初中三年级", "route": "素养路线"})
    check("steps: 初三物理素养", any(it["route"] == "素养路线" for it in s2["steps"]), s2["steps"])
    s3 = op_starting_steps({"name": "武术特长", "grade": "初二"})
    check("steps: 非五科不过滤", len(s3["steps"]) == 3, s3["steps"])
    s4 = op_starting_steps({"name": "阅读素养", "grade": "初二"})
    check("steps: 阅读素养三学年起步", s4["found"] and len(s4["steps"]) == 3, s4)

    g = hard_gate_check({"direction": "舞蹈特长", "grade": "初二", "facts": {
        "起学年级": "一年级", "已学时长": 7, "持续训练至今": True, "柔韧达标": True,
        "教育投入": "可", "城市": "上海"}})
    check("gate: 舞蹈窗口满足=可行", g["verdict"] == "可行", g)

    g = hard_gate_check({"direction": "舞蹈特长", "grade": "初二", "facts": {
        "起学年级": "初一", "教育投入": "可"}})
    check("gate: 初一起学=受限", g["verdict"] == "路线受限", g)

    g = hard_gate_check({"direction": "舞蹈特长", "grade": "初二", "facts": {}})
    check("gate: 无事实=待信息+ask", g["verdict"] == "待信息" and g["ask"], g)

    g = hard_gate_check({"direction": "舞蹈特长", "grade": "初三", "facts": {
        "起学年级": "一年级", "已学时长": 7, "持续训练至今": True, "柔韧达标": True,
        "教育投入": "可"}})
    check("gate: 初三转化窗口=有条件", g["verdict"] == "有条件可行" and g["limits"], g)

    g = hard_gate_check({"direction": "数学特长", "grade": "初三", "facts": {
        "教育投入": "可", "成绩": "优秀"}})
    check("gate: 初三数学竞赛关闭", g["verdict"] == "路线受限"
          and g["five_subject_route"] == "素养路线", g)

    g = hard_gate_check({"direction": "数学特长", "grade": "初一", "facts": {
        "成绩": "中等", "教育投入": "可"}})
    check("gate: 初一成绩中等→素养", g["five_subject_route"] == "素养路线"
          and g["verdict"] == "路线受限", g)

    g = hard_gate_check({"direction": "数学特长", "grade": "初一", "facts": {
        "成绩": "拔尖", "教育投入": "可"}})
    check("gate: 初一拔尖→竞赛可行", g["five_subject_route"] == "竞赛路线"
          and g["verdict"] == "可行", g)

    g = hard_gate_check({"direction": "水上运动特长", "grade": "初二", "facts": {
        "心肺": "有哮喘", "教育投入": "可"}})
    check("gate: 水上心肺受限", g["verdict"] == "路线受限" and g["limits"][0]["项"].startswith("安全"), g)

    g = hard_gate_check({"direction": "水上运动特长", "grade": "初二", "facts": {}})
    ask_text = " ".join(g["ask"])
    check("gate: 水上心肺必问置前", "心肺" in ask_text and g["ask"][0].find("心肺") >= 0, g["ask"])

    g = hard_gate_check({"direction": "美术特长", "grade": "初二", "facts": {
        "色觉": "色弱", "教育投入": "可"}})
    check("gate: 美术色弱受限", g["verdict"] == "路线受限", g)

    # v3.5.0 阅读素养软性素养项：不满足只降可行度、方向保留
    g = hard_gate_check({"direction": "阅读素养", "grade": "初二", "facts": {
        "读写耐力": False, "共情理解": False, "读写致用": False, "教育投入": "可"}})
    soft_limits = [l["项"] for l in g["limits"]
                   if l["项"] in ("读写耐力", "共情理解", "读写致用")]
    check("gate: 阅读素养软性项不满足=有条件可行",
          g["verdict"] == "有条件可行" and len(soft_limits) == 3, g)

    g = hard_gate_check({"direction": "阅读素养", "grade": "初二", "facts": {
        "读写耐力": True, "共情理解": True, "读写致用": True, "教育投入": "可"}})
    check("gate: 阅读素养全满足=可行", g["verdict"] == "可行" and not g["limits"], g)

    g = hard_gate_check({"direction": "阅读素养", "grade": "初二", "facts": {}})
    check("gate: 阅读素养无事实=待信息", g["verdict"] == "待信息" and g["ask"], g)

    # v3.5.0 已答未知：明确回答"不清楚/无法确定"= 已回答，不进 ask、不再追问
    # v3.5.3：缺项全为软性项且已有其余事实 → 有条件可行（初步判断）而非待信息
    g = hard_gate_check({"direction": "阅读素养", "grade": "初二", "facts": {
        "读写耐力": "现在无法确定", "共情理解": True, "读写致用": True, "教育投入": "可"}})
    check("gate: 软性项'无法确定'=初步判断不追问",
          g["verdict"] == "有条件可行"
          and "读写耐力" in [m["项"] for m in g["missing_required"]]
          and any(l["项"] == "初步判断" for l in g["limits"])
          and not any("翻几页" in q for q in g["ask"]), g)

    g = hard_gate_check({"direction": "美术特长", "grade": "初二", "facts": {
        "色觉": "不清楚", "教育投入": "可"}})
    check("gate: 色觉'不清楚'=待信息且ask为空",
          g["verdict"] == "待信息" and g["ask"] == [], g)

    # v3.5.3 用户场景：游泳其余条件满足、仅缺臂展 → 初步判断可行
    g = hard_gate_check({"direction": "水上运动特长", "grade": "初一", "facts": {
        "心肺": "无碍", "起学年级": "三年级", "持续训练至今": True, "教育投入": "中等"}})
    check("gate: 游泳仅缺臂展=初步判断可行",
          g["verdict"] == "有条件可行"
          and [m["项"] for m in g["missing_required"]] == ["臂展"]
          and any(l["项"] == "初步判断" and "臂展待确认" in l["口径"]
                  for l in g["limits"]), g)

    # v3.5.3 回归：年级开关由 grade 满足不算已答事实——五科空事实仅凭年级
    # 不得升级初步判断；成绩位置未采集按库内口径"不许写结论"保持待信息
    g = hard_gate_check({"direction": "信息学特长", "grade": "初一", "facts": {}})
    check("gate: 五科空事实=待信息（年级开关不算已答）",
          g["verdict"] == "待信息"
          and "成绩位置" in [m["项"] for m in g["missing_required"]], g)

    g = hard_gate_check({"direction": "信息学特长", "grade": "初一", "facts": {
        "教育投入": "可"}})
    check("gate: 五科缺成绩位置=待信息（核心项不许初步结论）",
          g["verdict"] == "待信息"
          and [m["项"] for m in g["missing_required"]] == ["成绩位置"], g)

    g = hard_gate_check({"direction": "水上运动特长", "grade": "初二", "facts": {
        "心肺": "无碍", "教育投入": "可", "已学时长": "记不清了"}})
    check("gate: 窗口'记不清'=待信息不追问",
          g["verdict"] == "待信息"
          and not any("学了多久" in q or "几年级" in q for q in g["ask"]), g)

    # 收敛：P3 池 + 兴趣归库（舞蹈）首位（items facts 齐 → ask_union 空 → final）
    fp = full_packages({
        "tags": [3],
        "items": [
            {"name": "舞蹈特长", "grade": "初二", "is_interest": True, "facts": {
                "起学年级": "一年级", "已学时长": 7, "持续训练至今": True,
                "柔韧达标": True, "教育投入": "可"}},
            {"name": "田径特长", "grade": "初二", "facts": {"心肺": "无碍", "教育投入": "可"}},
            {"name": "水上运动特长", "grade": "初二", "facts": {"心肺": "无碍", "教育投入": "可"}},
            {"name": "球类运动特长", "grade": "初二", "facts": {"心肺": "无碍", "教育投入": "可"}},
            {"name": "武术特长", "grade": "初二", "facts": {"教育投入": "可"}},
        ]})
    check("收敛: ≤3", len(fp["kept"]) == 3 and len(fp["dropped"]) == 2, fp["kept"])
    check("收敛: 兴趣首位", fp["kept"][0]["name"] == "舞蹈特长"
          and fp["interest"]["name"] == "舞蹈特长", [k["name"] for k in fp["kept"]])
    check("收敛: 可行度排序（武术待信息垫底被砍）",
          "武术特长" in [d["name"] for d in fp["dropped"]], fp["dropped"])
    check("收敛: 包内全版块", all("advantage" in k and "competitions" in k
                                 and "steps" in k and "gate" in k for k in fp["kept"]))

    # 两阶段收敛：双标签 + 兴趣（水上属 P3，不在 ④⑥ 池内 → 占首位席不挤标签保底）
    fp4 = full_packages({
        "tags": [4, 6],
        "items": [{"name": n, "grade": "初二",
                   **({"is_interest": True} if n == "水上运动特长" else {}), "facts": {}}
                  for n in ["水上运动特长", "美术特长", "音乐特长",
                            "思辨能力", "商业洞察", "社会认知"]]})
    k4 = [k["name"] for k in fp4["kept"]]
    check("收敛: 兴趣首位+双标签各保底", k4 == ["水上运动特长", "美术特长", "思辨能力"], k4)

    # v3.5.4 图3 场景防回归（初三 P3+P5+篮球兴趣零基础）：P5 必有代表、
    # 心肺安全类置顶 ask_union、球类/田径待补充项含心肺——平台实测曾漏。
    # v3.7.0 拆两场景：collect（facts 空，只问不结）+ 拒答自动解锁
    # （心肺"不清楚"→ 问句清空 → phase=final 全版块，missing 仍按待补充登记）
    fp_b3 = full_packages({"tags": [3, 5], "grade": "初三",
                           "items": [{"name": "球类运动特长", "grade": "初三",
                                      "is_interest": True, "facts": {}}]})
    kb3 = [k["name"] for k in fp_b3["kept"]]
    check("收敛: P3+P5+篮球兴趣=P5代表入选（禁止自选全体育）",
          kb3[0] == "球类运动特长" and "小语种能力培养" in kb3
          and "武术特长" not in kb3, kb3)
    check("两阶段: 条件未齐=collect（只问不结）",
          fp_b3["phase"] == "collect" and fp_b3["ask_union"]
          and "指令" in fp_b3
          and all(all(kk not in k for kk in ("brief", "competitions",
                                             "advantage", "steps"))
                  for k in fp_b3["kept"]),
          {"phase": fp_b3.get("phase"), "ask_union": fp_b3.get("ask_union")})
    check("gate: 体育方向待补充含心肺（安全类不可漏）",
          all(any("安全类条件" in m["项"] for m in k["gate"]["missing_required"])
              for k in fp_b3["kept"] if k["name"] in ("球类运动特长", "田径特长")),
          {k["name"]: [m["项"] for m in k["gate"]["missing_required"]]
           for k in fp_b3["kept"]})
    check("gate: 心肺问句置顶 ask_union",
          fp_b3["ask_union"] and "心肺" in fp_b3["ask_union"][0], fp_b3["ask_union"])
    fp_b3f = full_packages({"tags": [3, 5], "grade": "初三",
                            "facts": {"心肺": "不清楚", "教育投入": "可",
                                      "家庭规划": "不清楚"},
                            "items": [{"name": "球类运动特长", "grade": "初三",
                                       "is_interest": True, "facts": {}}]})
    check("两阶段: 家长拒答（不清楚）自动解锁终版",
          fp_b3f["phase"] == "final" and fp_b3f["ask_union"] == []
          and all(all(kk in k for kk in ("gate", "brief", "competitions",
                                         "advantage", "steps"))
                  for k in fp_b3f["kept"])
          and all(any("安全类条件" in m["项"] for m in k["gate"]["missing_required"])
                  for k in fp_b3f["kept"]
                  if k["name"] in ("球类运动特长", "田径特长")),
          {"phase": fp_b3f.get("phase"), "ask_union": fp_b3f.get("ask_union")})
    check("advantage: 多方向包内含 summary",
          all("summary" in k["advantage"] for k in fp_b3f["kept"])
          and fp_b3f["kept"][0]["advantage"]["summary"], None)

    fp5 = full_packages({
        "tags": [4, 6],
        "items": [{"name": n, "grade": "初二", "facts": {}}
                  for n in ["美术特长", "音乐特长", "思辨能力", "商业洞察", "社会认知"]]})
    k5 = [k["name"] for k in fp5["kept"]]
    check("收敛: 无兴趣双标签保底+池序补足", k5 == ["美术特长", "思辨能力", "音乐特长"], k5)

    fp6 = full_packages({"tags": [4], "items": [
        {"name": n, "grade": "初一", "facts": {}} for n in ["美术特长", "音乐特长"]]})
    k6 = [k["name"] for k in fp6["kept"]]
    check("收敛: 池不足不硬凑", k6 == ["美术特长", "音乐特长"] and len(fp6["dropped"]) == 0, k6)

    # 池自动补全：模型只传兴趣方向（不传全池）→ 脚本按 tags 补全候选，标签保底仍生效
    fp7 = full_packages({
        "tags": [4, 6], "grade": "初一",
        "items": [{"name": "水上运动特长", "is_interest": True, "facts": {}}]})
    k7 = [k["name"] for k in fp7["kept"]]
    check("收敛: 只传兴趣也保底覆盖双标签", k7 == ["水上运动特长", "美术特长", "思辨能力"], k7)

    fp8 = full_packages({"tags": [4], "grade": "初一"})
    check("收敛: 零items纯池补全", [k["name"] for k in fp8["kept"]] == ["美术特长", "音乐特长"],
          [k["name"] for k in fp8["kept"]])

    # v3.5.0 阅读素养单方向全量包（家长定向询问/兴趣归库场景）
    fp9 = full_packages({"items": [{"name": "阅读素养", "grade": "初二", "is_interest": True,
                                    "facts": {"读写耐力": True, "共情理解": True,
                                              "读写致用": True, "教育投入": "可"}}]})
    k9 = fp9["kept"][0]
    check("全包: 阅读素养四类数据齐全",
          len(fp9["kept"]) == 1
          and fp9["interest"]["name"] == "阅读素养"
          and k9["gate"]["verdict"] == "可行"
          and k9["advantage"]["found"] and len(k9["advantage"]["dimensions"]) == 4
          and k9["competitions"]["count"] == 8 and k9["steps"]["found"]
          and k9["brief"],
          {kk: k9.get(kk) for kk in ("gate", "advantage", "competitions", "steps", "brief")})

    # 懒加载：跨大类收敛只加载涉及大类的细粒度文件（数理+艺术各 5 类型）
    _CAT_CACHE.clear()
    full_packages({"tags": [1, 4], "grade": "初一"})
    expect_keys = sorted([("数理", d) for d in DTYPES] + [("艺术", d) for d in DTYPES])
    check("懒加载: P1+P4 只载数理+艺术两类×5文件",
          sorted(_CAT_CACHE) == expect_keys, sorted(_CAT_CACHE))

    fp2 = full_packages({"items": [{"name": "数学特长", "grade": "初二", "info": True}]})
    check("info 模式: 不判定不问", fp2["kept"][0]["gate"] is None
          and fp2["has_ask"] is False, fp2["kept"][0].get("gate"))

    fp3 = full_packages({"tags": [1], "items": [
        {"name": n, "grade": "初一", "facts": {"成绩": "优秀", "教育投入": "可"}}
        for n in ["数学特长", "信息学特长", "物理特长"]]})
    check("五科批量: 全可行", all(k["gate"]["verdict"] == "可行" for k in fp3["kept"]), fp3)

    # 懒加载：赛事详情检索只加载六大赛事库（不带其他类型）
    _CAT_CACHE.clear()
    dd = op_competition_detail({"text": "高联"})
    check("懒加载: 赛事检索只载6个赛事库仍命中",
          dd["matched_fullname"] is not None
          and len(_CAT_CACHE) == 6
          and all(k[1] == "赛事" for k in _CAT_CACHE), sorted(_CAT_CACHE))

    # v3.5.1 平台 planner facts 键表（2026-09-18 游泳对话真实 payload 回放）
    pf = _planner_facts({"city": "杭州", "grade": "初一", "health": "无身体问题",
                         "arm_span": "未测", "investment": "约1w/年",
                         "learning_start": "未学过"}, "初一")
    check("planner: 游泳对话 facts 六键全落地",
          pf.get("心肺") == "无碍" and pf.get("零基础") == "是"
          and pf.get("教育投入") == "约1w/年" and pf.get("城市") == "杭州"
          and pf.get("臂展达标") == "未测", pf)

    r = main({"parameters": {"direction": "游泳特长", "grade": "初一", "facts": {
        "city": "杭州", "grade": "初一", "health": "无身体问题",
        "arm_span": "未测", "investment": "约1w/年", "learning_start": "未学过"}}})
    g10 = r["kept"][0]["gate"]
    check("planner: 游泳全答后=路线受限且零追问",
          g10["verdict"] == "路线受限" and g10["ask"] == []
          and g10["missing_required"] == []
          and any(m["项"] == "起学窗口" for m in g10["limits"]), g10)

    pf2 = _planner_facts({"health": "没有哮喘", "learning_start": "从三年级开始学了5年",
                          "arm_span": "比身高多10cm"}, "")
    check("planner: 否定式心肺+起学年级+臂展提取",
          pf2.get("心肺") == "无碍" and pf2.get("起学年级") == "三年级"
          and pf2.get("已学时长") == 5 and pf2.get("臂展达标") == "是", pf2)

    # v3.5.3 平台第二套 facts 键名（2026-09-18 游泳对话第二份日志真实 payload
    # 回放：start_grade/duration/annual_investment/city/health/arm_span）
    pf4 = _planner_facts({"health": "健康，无哮喘或心脏问题，无医生禁止剧烈运动",
                          "start_grade": "三年级", "duration": "持续学习未间断",
                          "arm_span": "未测量", "city": "杭州",
                          "annual_investment": "约1w"}, "初中")
    check("planner: 第二套键名（start_grade/duration/annual_investment）全落地",
          pf4.get("心肺") == "无碍" and pf4.get("起学年级") == "三年级"
          and pf4.get("持续训练至今") is True
          and pf4.get("教育投入") == "约1w" and pf4.get("城市") == "杭州"
          and pf4.get("臂展达标") == "未测量", pf4)

    r4 = main({"parameters": {"direction": "游泳特长", "grade": "初中", "facts": {
        "health": "健康，无哮喘或心脏问题，无医生禁止剧烈运动",
        "start_grade": "三年级", "duration": "持续学习未间断",
        "arm_span": "未测量", "city": "杭州", "annual_investment": "约1w"}}})
    g11 = r4["kept"][0]["gate"]
    check("planner: 游泳第二套键名全答+臂展未测=初步判断且零追问",
          g11["verdict"] == "有条件可行" and g11["ask"] == []
          and [m["项"] for m in g11["missing_required"]] == ["臂展"]
          and any(l["项"] == "初步判断" and "臂展待确认" in l["口径"]
                  for l in g11["limits"]), g11)

    pf3 = _planner_facts({"health": "有哮喘"}, "")
    check("planner: 异常心肺原样传入（宁严勿漏）", pf3.get("心肺") == "有哮喘", pf3)

    # v3.5.4 planner 三形态（2026-09-21 推荐流程真实日志回放；此前标签池/
    # 操作名形态全部 no_action_detected → 结论轮拿不到池/优势/赛事/ask_union）
    rt = main({"args": {}, "operation": "tags"})
    check("planner: 操作名形态（operation 键）执行 tags",
          isinstance(rt.get("labels"), list) and len(rt["labels"]) == 6, rt)
    rt2 = main({"parameters": {"grade": "三年级", "traits": "喜欢运动",
                               "operation": "tags"}})
    check("planner: 操作名形态（parameters 内）执行 tags",
          isinstance(rt2.get("labels"), list) and len(rt2["labels"]) == 6, rt2)
    rt3 = main({"tags": []})
    check("planner: 标签轮 tags:[] 空数组 → 展示 6 标签（v3.7.0，此前 no_action_detected）",
          isinstance(rt3.get("labels"), list) and len(rt3["labels"]) == 6
          and "展示指引" in rt3, rt3)
    check("tags: 出参带展示指引（兜底邀约不得省略）",
          "展示指引" in t and "不太像" in t["展示指引"], t.get("展示指引"))
    check("tags: 展示指引含已展示分支（禁止重发、改归类确认）",
          "禁止重发" in t["展示指引"] and "归类确认" in t["展示指引"],
          t.get("展示指引"))

    # v3.7.0 planner 新形态（2026-09-22 两份测试日志回放暴露的缺口）
    rsel = main({"parameters": {"grade": "四年级", "selected_tag": "6",
                                "facts": {"兴趣线索": "学过钢琴但不喜欢，已放弃",
                                          "孩子年级": "四年级"}}})
    check("planner: selected_tag:\"6\" → P6 池 collect（日志2第6轮，此前 "
          "no_action_detected → 模型拿不到问句、没问条件）",
          rsel.get("phase") == "collect"
          and [k["name"] for k in rsel.get("kept", [])]
          == ["思辨能力", "商业洞察", "社会认知"]
          and len(rsel.get("ask_union", [])) >= 1,
          {"phase": rsel.get("phase"),
           "kept": [k["name"] for k in rsel.get("kept", [])],
           "ask_union": rsel.get("ask_union")})
    rsel2 = main({"parameters": {"grade": "初一",
                                 "selected_tags": ["⑥", 4]}})
    check("planner: selected_tags 复数/⑥/int 混写同权归池",
          rsel2.get("pool") == ["思辨能力", "商业洞察", "社会认知",
                                "美术特长", "音乐特长"],
          rsel2.get("pool"))
    rcls = main({"parameters": {"grade": "四年级",
                                "interest_clue": "学过钢琴但不喜欢，已放弃",
                                "user_response": {"length": 4}},
                 "facts": {}, "grade": "四年级"})
    check("planner: 归类轮兜底（grade 真实值+无 stage → tags 操作，日志2第4轮，"
          "此前 no_action_detected → 模型凭记忆展示残缺选项）",
          isinstance(rcls.get("labels"), list) and len(rcls["labels"]) == 6
          and "clues" in rcls and "禁止重发" in rcls.get("展示指引", ""),
          {"keys": sorted(rcls.keys())})
    rinvt = main({"facts": {}, "grade": "初一", "stage": "前置采集"})
    rcol = main({"parameters": {"grade": "四年级"}, "stage": "collect",
                 "facts": {}})
    check("planner: 盘点轮（stage=前置采集/collect）不喂标签数据，防跳过尝试盘点",
          rinvt.get("error") == "no_action_detected"
          and rcol.get("error") == "no_action_detected",
          {"前置采集": rinvt.get("error"), "collect": rcol.get("error")})
    rph = main({"parameters": {"tags": "<user_input>", "grade": "<user_input>"},
                "tags": "<user_input>", "facts": {}, "grade": "<user_input>"})
    check("planner: 首轮占位符不喂标签数据（防跳过年级轮）",
          rph.get("error") == "no_action_detected", rph.get("error"))

    rp = main({"tags": ["③", "④", "⑤"], "grade": "三年级",
               "traits": ["爱运动", "喜欢唱歌"]})
    kp = rp["kept"][0] if rp.get("kept") else {}
    check("planner: 标签池形态两阶段锁（真实日志回放：ask 未齐不返回结论版块）",
          rp.get("phase") == "collect"
          and len(rp.get("kept", [])) == 3 and len(rp.get("ask_union", [])) >= 1
          and "指令" in rp
          and "大方向定性" in rp["指令"] and "分两轮" in rp["指令"]
          and "禁止输出具体方向推荐结论" in rp["指令"]
          and all(kk not in kp for kk in ("brief", "competitions",
                                          "advantage", "steps"))
          and all(all(kk not in k for kk in ("brief", "competitions",
                                             "advantage", "steps"))
                  for k in rp.get("kept", [])),
          {"phase": rp.get("phase"),
           "kept": [k["name"] for k in rp.get("kept", [])],
           "keys": sorted(kp.keys()),
           "ask_union": rp.get("ask_union")})
    # v3.7.1 模型通道：没学过的池方向 items 带零基础 → 预设性问句消失
    # （2026-09-22 第三轮测试反馈：对没学过的孩子问"几年级开始学的"是错问；
    # 注意必须走 action 形态——裸 tags payload 走 planner 适配层会重建 items）
    rzb = main({"action": "full_packages", "tags": ["③"], "grade": "初一",
                "items": [{"name": d, "grade": "初一", "facts": {"零基础": "是"},
                           "is_interest": False}
                          for d in ("田径特长", "舞蹈特长", "水上运动特长",
                                    "球类运动特长", "武术特长")]})
    check("模型通道: 全池零基础 items → 起学/时长/柔韧/臂展预设性问句消失"
          "（v3.7.1，只剩安全类+城市/投入）",
          rzb.get("phase") == "collect"
          and not any(w in q for q in rzb.get("ask_union", [])
                      for w in ("几年级开始学", "学了多久", "持续练",
                                "柔韧", "臂展"))
          and any("心肺" in q for q in rzb.get("ask_union", []))
          and any("城市" in q for q in rzb.get("ask_union", [])),
          {"ask_union": rzb.get("ask_union"),
           "verdicts": [(k["name"], k["gate"]["verdict"])
                        for k in rzb.get("kept", [])]})

    rpf = main({"tags": ["③", "④", "⑤"], "grade": "三年级",
                "facts": {"心肺": "不清楚", "教育投入": "可",
                          "家庭规划": "不清楚", "读写耐力": "不清楚",
                          "色觉": "不清楚"},
                "traits": ["爱运动", "喜欢唱歌"]})
    kpf = rpf["kept"][0] if rpf.get("kept") else {}
    check("planner: 标签池形态拒答自动解锁终版（v3.7.0 删 force_final 开关）",
          rpf.get("phase") == "final"
          and all(kk in kpf for kk in ("gate", "brief", "competitions",
                                       "advantage", "steps"))
          and kpf["advantage"]["found"] and kpf["competitions"]["found"],
          {"phase": rpf.get("phase"), "keys": sorted(kpf.keys()),
           "ask_union": rpf.get("ask_union")})

    # v3.6.0 平台第三套 facts 键名（2026-09-22 美术对话日志：
    # 年投入/孩子年级/色觉正常:布尔——此前全丢 → gate 卡待信息还重发已答问题）
    pf5 = _planner_facts({"城市": "杭州", "年投入": "2w", "孩子年级": "初一",
                          "色觉正常": True}, "")
    check("planner: 第三套键名（年投入/孩子年级/色觉正常布尔）全落地",
          pf5.get("城市") == "杭州" and pf5.get("教育投入") == "2w"
          and pf5.get("grade") == "初一" and pf5.get("色觉") == "正常", pf5)
    pf6 = _planner_facts({"色觉正常": False, "心肺正常": True}, "")
    check("planner: 布尔 False→异常值（宁严勿漏）",
          pf6.get("色觉") == "色弱" and pf6.get("心肺") == "无碍", pf6)

    r5 = main({"parameters": {"direction": "美术特长", "grade": "初一",
                              "facts": {"城市": "杭州", "年投入": "2w",
                                        "孩子年级": "初一", "色觉正常": True}}})
    g5 = r5["kept"][0]["gate"]
    check("planner: 美术第三套键名全答后=可行零追问（日志A第4轮回放）",
          g5["verdict"] == "可行" and g5["ask"] == []
          and g5["missing_required"] == [] and g5["limits"] == [], g5)
    check("planner: 单方向形态不锁（SOFT_MISSING 初步结论口径不变）+ notice",
          "brief" in r5["kept"][0] and "notice" in r5 and r5["phase"] == "final",
          {"keys": sorted(r5["kept"][0].keys()), "notice": r5.get("notice")})

    # v3.6.0 M5 闭环回放（日志B 推荐流程：collect 锁 → 家长补答 → final 终版）
    rb0 = main({"parameters": {"tags": ["P1", "P5"], "grade": "初一"}})
    check("planner: 日志B结论轮=collect 锁（只问句零结论）",
          rb0.get("phase") == "collect" and "指令" in rb0
          and all("brief" not in k for k in rb0["kept"])
          and len(rb0["ask_union"]) >= 2, rb0.get("ask_union"))
    rb1 = main({"parameters": {"tags": ["P1", "P5"], "grade": "初一",
                               "facts": {"成绩位置": "班里中上", "城市": "杭州",
                                         "年投入": "2w", "家庭规划": "接受"}}})
    check("planner: 日志B补答后重调=final 全版块（成绩位置/家庭规划前瞻键落地）",
          rb1.get("phase") == "final" and not rb1.get("ask_union")
          and all(kk in rb1["kept"][0] for kk in ("brief", "competitions",
                                                  "advantage", "steps")),
          {"phase": rb1.get("phase"), "ask_union": rb1.get("ask_union"),
           "verdicts": [k["gate"]["verdict"] for k in rb1["kept"]]})
    pf7 = _planner_facts({"成绩位置": "班里中上", "家庭规划": "接受"}, "初一")
    check("planner: 成绩位置/家庭规划键落地（数理 ask_union 前瞻）",
          pf7.get("成绩") == "班里中上" and pf7.get("家庭规划") == "接受", pf7)

    # v3.5.4 小学零基础窗口口径：小学在读零基础 = 现在起步、起学仍在窗口内，
    # 不再追问起学/持续/柔韧；初中在读零基础才按"窗口必不满足"受限
    gw = full_packages({"items": [{"name": "水上运动特长", "grade": "三年级",
                                   "facts": {"零基础": "是"}}]})["kept"][0]["gate"]
    check("窗口: 小学零基础=窗口过不追问（只问安全/臂展/投入）",
          gw["verdict"] == "待信息"
          and all("起学" not in m["项"] for m in gw["missing_required"])
          and not any(re.search(r"(学了多久|起学|柔韧|还在学|持续)", q)
                      for q in gw["ask"]), gw)
    gj = full_packages({"items": [{"name": "水上运动特长", "grade": "初一",
                                   "facts": {"零基础": "是"}}]})["kept"][0]["gate"]
    check("窗口: 初中零基础=起学窗口必不满足（受限）",
          gj["verdict"] == "路线受限"
          and any(m["项"] == "起学窗口" for m in gj["limits"]), gj)

    # v3.5.2 跨流程承接：full_packages 顶层 facts（全局通用项）
    rg = full_packages({"tags": ["P6"], "grade": "初一",
                        "facts": {"城市": "杭州", "教育投入": "可", "心肺": "无碍",
                                  "零基础": "是"},  # 零基础=方向语境键，不应打底
                        "items": [{"name": "水上运动特长", "grade": "初一",
                                   "facts": {"零基础": "是", "臂展达标": "未测"},
                                   "is_interest": True}]})
    by_g = {k["name"]: k for k in rg["kept"]}
    check("全局facts: 池补全候选吃到城市/教育投入（ask 收敛）",
          all("城市" not in q and "教育投入" not in q
              for k in rg["kept"] for q in k["gate"].get("ask", [])),
          [(k["name"], k["gate"].get("ask")) for k in rg["kept"]])
    check("全局facts: 水上零基础只认 item 级（gate 正常判定）",
          by_g["水上运动特长"]["gate"]["verdict"] == "路线受限",
          by_g["水上运动特长"]["gate"])
    # 方向语境键不打底（确定性命中：无 tags 双 items，两个都进 kept；
    # 舞蹈若吃到顶层零基础会判"初一零基础=路线受限"）
    rz = full_packages({"grade": "初一",
                        "facts": {"零基础": "是", "城市": "杭州",
                                  "教育投入": "可", "心肺": "无碍"},
                        "items": [{"name": "水上运动特长", "grade": "初一",
                                   "facts": {"零基础": "是", "臂展达标": "未测"},
                                   "is_interest": True},
                                  {"name": "舞蹈特长", "grade": "初一"}]})
    bz = {k["name"]: k for k in rz["kept"]}
    check("全局facts: 方向语境键不打底（舞蹈不吃水上零基础）",
          "水上运动特长" in bz and "舞蹈特长" in bz
          and bz["水上运动特长"]["gate"]["verdict"] == "路线受限"
          and bz["舞蹈特长"]["gate"]["verdict"] != "路线受限",
          {k: bz[k]["gate"]["verdict"] for k in bz})
    # 不传顶层 facts 时行为与 v3.5.1 一致（守恒锚点）
    rg0 = full_packages({"tags": ["P6"], "grade": "初一",
                         "items": [{"name": "水上运动特长", "grade": "初一",
                                    "facts": {"零基础": "是", "臂展达标": "未测"},
                                    "is_interest": True}]})
    n0 = len(rg0["ask_union"])
    check("全局facts: 不传时行为不变（ask_union 非空，池补全仍问城市/投入）",
          n0 >= 1 and any("城市" in q or "教育投入" in q for q in rg0["ask_union"]),
          rg0["ask_union"])

    print("\nsmoke: %d/%d PASS" % (ok[0], total[0]))
    return ok[0] == total[0]


_OP_MARK = re.compile(r"skill_tools[.：:]?\s*([a-z_]+)\s*\(\s*(\{.*?\})\s*\)", re.S)


def _norm_op(s):
    """操作名归一：剥掉可能的 skill_tools. 前缀（"skill_tools.tags" → "tags"）。"""
    s = s.strip()
    return s[len("skill_tools."):] if s.startswith("skill_tools.") else s


def _looks_like_cmd(d):
    """判断 dict 是否像一个命令对象（包装键递归前的保守判断，避免误入无关嵌套）：
    含 action/op/操作 键，或 tool/script 键的值是已知操作名。"""
    if any(k in d for k in ("action", "op", "操作")):
        return True
    for k in ("tool", "script"):
        v = d.get(k)
        if isinstance(v, str) and _norm_op(v) in OPS:
            return True
    return False


def _extract_op_args(payload):
    """从上下文里解析出 (操作名, 参数dict)。
    优先级：结构化命令对象 > 文本标记 > 平台 planner 协议。

    平台经 stdin 传入的"本轮 JSON 上下文"不保证是干净的 {"action": …}，可能是：
    ① 直接命令 {"action": 操作名, …参数}；
    ② MCP 风格 {"tool": 操作名, "args": {参数}}（含 skill_tools 包装：
       {"skill_tools": {"tool": …, "args": …}}）；
    ③ 命令嵌在包装键（skill_tools/tool/script/args）下的嵌套对象；
    ④ 整段对话文本——模型在其中写了 `skill_tools.操作名({...})` 调用标记；
    ⑤ 平台 planner 调用（无 action、带 parameters/planner_parameters，
       平台已从用户消息提取 direction/grade/facts——2026-09-16 平台对话实测确认）。
    这里递归解析：先找结构化命令，再全文扫描调用标记，最后适配 planner 协议。
    """
    if isinstance(payload, dict):
        op = payload.get("action") or payload.get("op") or payload.get("操作")
        if not op:
            for key in ("tool", "script"):
                v = payload.get(key)
                if isinstance(v, str) and _norm_op(v) in OPS:
                    op = _norm_op(v)
                    break
        if op:
            a = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            extra = {k: v for k, v in payload.items()
                     if k not in ("action", "op", "操作", "skill_tools", "tool", "script", "args")}
            return str(op).strip(), {**extra, **a}
        for key in ("skill_tools", "tool", "script", "args"):
            v = payload.get(key)
            if isinstance(v, dict) and _looks_like_cmd(v):
                r = _extract_op_args(v)
                if r[0] is not None:
                    return r
    best = [None, None]

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            for m in _OP_MARK.finditer(o):
                best[0], best[1] = m.group(1), m.group(2)

    walk(payload)
    if best[0]:
        try:
            return best[0], json.loads(best[1])
        except json.JSONDecodeError:
            return best[0], {"query": best[1]}
    return _planner_call(payload)


def _planner_call(payload):
    """平台 planner 协议适配：无 action/标记时，读 parameters/planner_parameters。

    平台 planner 每轮把用户消息（哈希化）连同提取好的参数经 stdin 传入，
    模型文本里的调用标记不可见——planner 参数是脚本唯一输入通道
    （2026-09-21 完整日志确认三种形态，v3.5.4 前只认 direction 一种，
    其余全部 no_action_detected → 结论轮拿不到池/优势/赛事/ask_union）：
    ① 操作名形态——parameters/顶层带 operation/action 键（如 {"operation":
       "tags"}）→ 按该操作执行，其余键并入参数；
    ② 单方向形态——direction 存在 → full_packages 单方向完整数据包
       （gate 判定 + 说明/赛事/优势/起步），facts 为空时 gate 给出待信息 +
       ask（模型据此向家长提问）；
    ③ 标签池形态——tags 存在（P3/③/3 写法皆可）、无 direction →
       full_packages 多方向包（池自动补全 + 两阶段收敛 + ask_union）。
       v3.7.0：selected_tag/selected_tags（"6"/⑥/P6/int，单选或多选）
       与 tags 同权归入本形态（2026-09-22 日志2：家长选标签⑥后参数只带
       selected_tag:"6"）。零基础是"孩子什么都没学过"的孩子级事实 →
       扇出给全部池方向（模型通道仍只认 item 级，v3.5.2 白名单隔离不变）；
       已学时长/起学年级等方向语境键无方向绑定，不扇出。平台 items 是
       版块名数组（["brief","competitions",…]）不是方向列表，不透传。
    ④ 标签轮形态——tags:[] 空数组（尚未选任何标签）→ tags 操作。
    ⑤ 归类轮兜底——年级真实值 + 未声明 stage + ①-④ 全不中 → tags 操作
       （家长已看过标签、正在描述孩子情况的归类轮；stage=前置采集/
       collect 的盘点轮与年级占位符首轮不喂标签数据，防跳步）。
    非 planner 结构返回 (None, None)。
    """
    if not isinstance(payload, dict):
        return None, None
    params = payload.get("parameters")
    if not isinstance(params, dict):
        params = payload.get("planner_parameters")
    if not isinstance(params, dict):
        params = None

    def _op_from(src):
        for k in ("action", "op", "operation", "操作", "tags_action"):
            v = src.get(k)
            if isinstance(v, str) and _norm_op(v) in OPS:
                a = {kk: vv for kk, vv in src.items()
                     if kk not in ("action", "op", "operation", "操作", "tags_action", "args")}
                if isinstance(src.get("args"), dict):
                    a = {**a, **src["args"]}
                return _norm_op(v), a
        return None

    # ① 操作名形态：parameters 内或顶层
    for src in (params, payload):
        if isinstance(src, dict):
            r = _op_from(src)
            if r:
                return r

    # ② 单方向形态（v3.4.1 既有）
    p = params if isinstance(params, dict) and params.get("direction") else None
    if p is None:
        p = {k: payload[k] for k in ("direction", "grade", "facts") if k in payload}
    if p.get("direction"):
        direction = str(p.get("direction")).strip()
        grade = p.get("grade")
        if not isinstance(grade, str) or not grade.strip() or grade.strip().startswith("<"):
            grade = ""  # 平台未提取到时填 "<user_input>" 占位符
        else:
            grade = grade.strip()
        facts = _planner_facts(p.get("facts"), grade)
        return "full_packages", {"items": [{
            "name": direction, "grade": grade, "facts": facts, "is_interest": False}]}

    # ③ 标签池形态（推荐主流程结论轮）
    src = params if isinstance(params, dict) else payload
    raw = src.get("tags") if isinstance(src.get("tags"), list) else payload.get("tags")
    # v3.7.0 平台另有 selected_tag/selected_tags 形态（2026-09-22 日志2实测：
    # 家长选标签⑥后 parameters 只带 selected_tag:"6"、无 tags → 此前落
    # no_action_detected，模型拿不到池/ask_union → 没问条件直接糊结论）
    sel = []
    for k in ("selected_tag", "selected_tags"):
        v = src.get(k, payload.get(k))
        if isinstance(v, list):
            sel += [str(x) for x in v]
        elif isinstance(v, (str, int)) and str(v).strip():
            sel.append(str(v))
    tags = _norm_tags((raw if isinstance(raw, list) else []) + sel)
    if tags:
        grade = src.get("grade") or payload.get("grade")
        if not isinstance(grade, str) or not grade.strip() or grade.strip().startswith("<"):
            grade = ""
        else:
            grade = grade.strip()
        mapped = _planner_facts(src.get("facts") or payload.get("facts"), grade)
        facts = {k: v for k, v in mapped.items() if k in GLOBAL_FACT_KEYS}
        zero = mapped.get("零基础")
        items = [{"name": d, "grade": grade,
                  "facts": {"零基础": zero} if zero else {},
                  "is_interest": False}
                 for d in op_feature_pool({"tags": tags})["pool"]]
        return "full_packages", {"tags": tags, "grade": grade,
                                 "facts": facts, "items": items}

    # ④ 标签轮形态（v3.7.0）：平台标签轮传 tags:[]（空数组，尚未选任何标签）
    # → 返回 tags 操作展示 6 个标签（此前落 no_action_detected → 模型自编问题）
    if isinstance(src.get("tags"), list) or isinstance(payload.get("tags"), list):
        return "tags", {}

    # ⑤ 归类轮兜底（v3.7.0）：planner 轮、年级已采集（真实值非占位符）、
    # 未声明 stage、上述形态全不中 → 返回 tags 操作（6 标签 + clues +
    # 展示指引）。覆盖"标签已展示、家长描述了孩子情况、参数只带 grade +
    # interest_clue/user_response"的归类轮（2026-09-22 日志2实测：此前
    # no_action_detected → 模型凭记忆展示残缺选项）。stage=前置采集/
    # collect 的盘点轮不喂标签数据，防止跳过尝试盘点直接展示标签；
    # 年级占位符（首轮）也不喂，防止跳过年级轮。
    stage = str(src.get("stage") or payload.get("stage") or "").strip()
    g = src.get("grade") or payload.get("grade")
    g = g.strip() if isinstance(g, str) else ""
    if g and not g.startswith("<") and not stage:
        return "tags", {}
    return None, None


def _planner_facts(raw, grade):
    """平台 planner facts 键 → 契约 facts 键（只做确定映射，不猜不推算）。

    键表按平台真实提取形态收录（2026-09-18 两份游泳对话日志 + 2026-09-21
    推荐流程日志确认，平台存在多套键名）：health/physical_condition/心肺/
    身体条件 → 心肺；learning_start/prior_experience/学习经历/过往经历/
    以前学过/学过/曾学过 → 零基础/已学时长/起学年级（"没有/没学过"类=零基础）；
    start_grade/起学年级/起始年级 → 起学年级（值本身是年级）；duration/
    学习时长 → 持续训练至今（含"持续/未间断"类字眼）或已学时长（"N年"）；
    investment/annual_investment/每年投入/教育投入 → 教育投入；city/所在城市
    → 城市；arm_span/臂展 → 臂展达标（明确是/否才映射，"未测"类按已答未知
    原样传入）；grade/年级 → grade（"<user_input>"等占位符忽略）。值归一：
    心肺正向词表 + 否定模式（"无身体问题/没有哮喘/医生没有禁止"→无碍，
    否则原样传入由判定按受限口径处理——安全类宁严勿漏）；学习经历零基础
    词表含"未学过"（平台用"未"、家长口语用"没"，两套都要认）。其余键
    （标签/特质/运动艺术倾向等提取噪音）不映射。
    """
    out = {}
    if grade:
        out["grade"] = grade
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        # v3.6.0 第三套键名（2026-09-22 美术对话日志确认）：布尔形态
        # 色觉正常:true / 心肺正常:true——True→正向口径，False→异常值
        # （色弱/异常，gate 按受限口径处理，安全类宁严勿漏）
        if isinstance(v, bool):
            if k in ("色觉正常", "色觉无异常", "色觉没问题"):
                out["色觉"] = "正常" if v else "色弱"
            elif k in ("听力正常", "听力无异常"):
                out["听力"] = "正常" if v else "异常"
            elif k in ("心肺正常", "身体健康", "无身体问题", "无心脏问题"):
                out["心肺"] = "无碍" if v else "异常"
            elif k in ("家庭规划", "读写耐力", "共情理解", "读写致用"):
                out[k] = v  # bool 直存，gate _truthy 处理
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        if k in ("grade", "年级", "孩子年级", "学生年级", "在读年级",
                 "current_grade") and not v.startswith("<"):
            out["grade"] = v
        elif k in ("physical_condition", "health", "health_condition", "心肺", "身体条件"):
            if any(w in v for w in ("正常", "无碍", "健康", "没问题", "良好")) or \
                    re.search(r"(没有|无|未|不)[^，。,;；]{0,8}"
                              r"(哮喘|心脏|疾病|问题|异常|病史|毛病|不适|禁忌|禁止|忌)", v):
                out["心肺"] = "无碍"
            else:
                out["心肺"] = v  # 原样传入，由判定按受限口径处理
        elif k in ("prior_experience", "learning_experience", "learning_start",
                   "学习经历", "过往经历", "以前学过", "学过", "曾学过"):
            if any(w in v for w in ("没学过", "没学", "没有学", "未学过", "未学",
                                    "没练过", "未练过", "零基础", "从零", "从没",
                                    "没有", "无")):
                out["零基础"] = "是"
                continue
            m = re.search(r"(\d+(?:\.\d+)?)\s*年", v)
            if m:
                out["已学时长"] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
            m2 = re.search(r"(?:从|自)?(小学一年级|小学二年级|小学三年级|小学四年级|"
                           r"小学五年级|小学六年级|初中一年级|初中二年级|初中三年级|"
                           r"七年级|八年级|九年级|幼儿园|初一|初二|初三|高一|高二|高三|"
                           r"一年级|二年级|三年级|四年级|五年级|六年级)"
                           r"(?:开始|起)?(?:学|练|训)", v)
            if m2:
                out["起学年级"] = m2.group(1)
        elif k in ("start_grade", "起学年级", "起始年级", "开始年级"):
            m = re.search(r"(小学一年级|小学二年级|小学三年级|小学四年级|"
                          r"小学五年级|小学六年级|初中一年级|初中二年级|初中三年级|"
                          r"七年级|八年级|九年级|幼儿园|初一|初二|初三|高一|高二|高三|"
                          r"一年级|二年级|三年级|四年级|五年级|六年级)", v)
            if m:
                out["起学年级"] = m.group(1)
        elif k in ("duration", "学习时长", "训练时长"):
            if any(w in v for w in ("持续", "未间断", "没间断", "没停", "未停",
                                    "还在学", "仍在学", "一直学", "一直练")):
                out["持续训练至今"] = True
            else:
                m = re.search(r"(\d+(?:\.\d+)?)\s*年", v)
                if m:
                    out["已学时长"] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
        elif k in ("investment", "annual_investment", "每年投入", "教育投入",
                   "年投入", "年度投入", "预算", "budget"):
            out["教育投入"] = v
        elif k in ("city", "所在城市", "城市", "location"):
            out["城市"] = v
        elif k in ("arm_span", "臂展"):
            if _unknown(v):
                out["臂展达标"] = v  # "未测/没测过" → 已答未知，不追问
            elif _truthy(v) is True or any(w in v for w in ("多 10", "多10", "超过", "达标")):
                out["臂展达标"] = "是"
            elif _truthy(v) is False or any(w in v for w in ("不够", "不足", "没多")):
                out["臂展达标"] = "否"
        elif k in ("成绩位置", "成绩", "academic_performance", "performance",
                   "school_performance"):
            # v3.6.0 前瞻：数理/五科 ask_union 问"成绩位置"，平台提取键名
            # 未实测过——别名与契约键名双收（值交 _norm_score 归一）
            if not v.startswith("<"):
                out["成绩"] = v
        elif k in GLOBAL_FACT_KEYS and k not in ("grade", "年级"):
            # 契约键名直传（家庭规划/色觉/听力/读写三项等）——占位符忽略
            if not v.startswith("<"):
                out[k] = v
    return out


def main(payload):
    """平台协议入口：stdin JSON → stdout JSON。返回单个结果 dict，一次只执行一个操作。"""
    op, args = _extract_op_args(payload)
    if op is None:
        return {"error": "no_action_detected", "available": sorted(OPS)}
    if op not in OPS:
        return {"error": "unknown_action", "action": op, "available": sorted(OPS)}
    try:
        return OPS[op](args or {})
    except Exception as e:  # noqa: BLE001 边界兜底，错误也以 JSON 返回
        return {"error": "action_failed", "action": op, "detail": "%s: %s" % (type(e).__name__, e)}


def _main_cli(argv):
    """本地调试入口（平台不依赖此处）：--smoke 冒烟 / `python skill_tools.py <op> '<json>'`。"""
    if len(argv) >= 2 and argv[1] == "--smoke":
        sys.exit(0 if _smoke() else 1)
    if len(argv) >= 2 and argv[1] not in ("-",):
        op, raw = argv[1], argv[2] if len(argv) >= 3 else ""
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            args = {"query": raw}
        print(json.dumps(main({"action": op, **args}), ensure_ascii=False))
        return 0
    print(json.dumps({"error": "no_input", "available": sorted(OPS)}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    # 平台协议优先：stdin 有内容即本轮上下文——合法 JSON 按对象解析；
    # 否则整体当一段文本，交给 _extract_op_args 的文本标记解析（宽容裸标记）；
    # 仅 stdin 为空 / 不可读时才走 CLI（--smoke / 本地调试）。
    data = None
    try:
        raw = sys.stdin.read()
        if raw and raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"content": raw}
    except Exception:  # noqa: BLE001 stdin 读取异常 → 走 CLI
        data = None
    if data is not None:
        print(json.dumps(main(data), ensure_ascii=False))
        sys.exit(0)
    sys.exit(_main_cli(sys.argv))