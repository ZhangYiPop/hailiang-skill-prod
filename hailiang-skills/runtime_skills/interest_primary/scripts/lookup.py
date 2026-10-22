#!/usr/bin/env python3
"""兴趣班校验：班名对齐 / 年龄硬验证 / 预算软说明 / 陪练强度 / 优势话术 / 方向班池查询。

平台脚本约定（stdin JSON → stdout JSON）：
  平台通过 stdin 传入本轮对话 JSON 上下文，脚本始终读取 stdin 并与命令行参数合并
  （CLI 值覆盖 stdin 同名字段；stdin 为终端时不阻塞等待）。stdin 中的 query 与
  messages[].content 可能是 {"sha256","length","preview"} 哈希摘要对象而非原文——
  非字符串一律跳过文本扫描，原文以 CLI --query 为准；结构化参数
  （parameters / planner_parameters：age / grade / budget / classes / directions 等）
  优先于文本扫描。stdout 只输出一个 JSON 对象。
  单文件自包含：全部数据内嵌于本文件 GENERATED DATA 段（由 references_04/05 经
  _build/gen_lookup_data.py 生成，verify_skill.py 校验一致），不 import 任何本地模块——
  平台沙箱环境仅保证入口脚本本身可执行，import 同目录数据模块会直接失败。

使用场景（见 SKILL.md）：
  - 流程一 e 步 / 流程三 c 步：推荐班逐班年龄验证与"预算可能不足"说明生成；
    T11 / 顺位组装传 directions（如 score.py 的 t11_directions）取 direction_packages 方向班池；
    流程三类目查询传 category / 类别 / 大类（v1.2.7 新增，十六测实测 planner 形态）或 classes
    里传类别词（如"艺术类"）取 category_packages 类别班池——按速查表「大类」列展开
    （艺术类→音乐表演/舞蹈体态/美术创作三目全部班），排序与方向包同规则；
  - 流程四 a 步：单班年龄判定（含风险加严口径）与安全提示取用；
  - 流程二：陪练强度查询（表内班直接返回，不问家长）。

输入适配（只做确定性映射，不猜测、不编造；结构化字段优先于文本扫描）：
  结构化字段（深度 ≤4，含 parameters / planner_parameters 等嵌套）：
    classes / class_names / interest_classes / 班级 / 兴趣班 → 班名列表或顿号分隔串
    directions / direction / t11_directions / 主修方向 → 标准方向名（理科思维/实验探究/
      体育竞技/艺术感知/语言沟通/人文素养）；传入班名（如"游泳"）自动归一到所属方向
    age / 年龄 → 数字或 "7岁"；grade / 年级 → "二年级" "2年级" "大班" 等（中文/阿拉伯数字均可）
    budget / 预算 → 数字或文本；数字 ≥1000 视为元自动换算万元（10000 → 1.0），
      <1000 视为万元（2.5 → 2.5）；文本 "1万" "8000" "8千" "5k" 照旧解析
    city / 城市 → 仅记录，不参与判定
  文本扫描（query / user_input / text / input / content 等字符串字段及用户消息 content；
  assistant / system 消息不扫描，防止历史推荐污染匹配；哈希摘要对象非字符串，自动跳过）：
    班名与口语别名（长词优先、重叠去重）；"7岁 / 7 周岁"；"二年级 / 3年级 / 大班" 等年级；
    "预算1万 / 一年8000 / 5k" 等预算表述。

输出契约（任何输入不抛异常、stdout 单 JSON、退出码恒 0；平台以 stdout 的 ok 字段判定
  执行状态，ok:false 会被记为脚本执行失败，故仅在"无任何可用数据"时才返回 ok:false）：
  ok: true  → age / age_source / grade / budget / budget_known / classes[] / unmatched /
              direction_packages[]（传入方向时）/ output_reminders（固定输出提醒）；
              年龄未采集时附 age_missing=true + followup（补问话术）——班级数据照常完整
              返回，不视为执行失败
  ok: false → classes_not_found（未识别到任何班名与方向；hint：回问家长具体班名，禁止猜）
              script_error（hint：按 references_04 速查表人工核对）
  语义要点：
  - age_ok=false 仅由年龄产生（生效年龄 = 速查表最早起始与 references_05 硬红线取严）；
  - age_status 含四态：below_floor（未到最早起始，age_ok=false，不可推荐）/
    below_recommended（已到最早起始、未到建议切入下限，照常推荐附偏早句）/
    ok（建议切入区间内）/ past_recommended（孩子已过建议切入上限——内容偏基础
    启蒙向，默认不作为新推荐，仅窗口内与未到下限的班不足时补位，v1.2.5）；
  - 预算永远不产生排除语义（仅参与 v1.2.12 四级阶梯分级殿后）：budget_sufficient=false
    时班级信息照常完整返回，budget_note 已生成"预算可能不足"现成话术；
    budget_known=false 时按区间列出、不降级；
  - direction_packages：该方向全部班级，按 references_08 第六节 v1.2.12 四级阶梯排序
    （第一级 年龄满足+预算充足 > 第二级 过窗+预算充足 > 第三级 年龄满足+预算不足 >
    第四级 过窗+预算不足殿后；级内年龄：切入区间内 > 未到切入下限；再按陪练强度低
    在前、年费下限低在前），逐班含年龄判定 / 年费 / 预算话术 / 陪练 / 优势，
    T11 与顺位选班直接取用；
  - 风险独有班（速查表外）in_speed_table=false，费用/陪练/优势为 null，
    附"未收录在精选指导表中"提示，禁止编造其费用。
  - 类型库收录但速查表未收录的班（v1.0.6 全量补充后 7 项：中考体育集训、亲子游泳、
    美术艺考、传媒艺考、音乐艺考、奥尔夫音乐启蒙、亲子绘本阅读）同样返回
    in_speed_table=false 条目（matched=true、无年龄/费用/陪练/优势数据），
    不再触发 classes_not_found；"（低龄段）"后缀自动归一。

调试（命令行仅为兼容保留，未知参数静默忽略）：
  python3 lookup.py --classes "画画,钢琴" --age 5 --budget 1
  python3 lookup.py --directions "艺术感知,体育竞技" --age 8
  python3 lookup.py --selftest
"""
import argparse
import json
import re
import sys

# ==== GENERATED DATA BEGIN（本段由 _build/gen_lookup_data.py 生成，请勿手改）====
# 数据源：references_04（75 班主表 + 班名对照）与 references_05（硬红线 + 风险独有班）。
# 由 _build/gen_lookup_data.py 解析生成，verify_skill.py 交叉校验；请勿手改本段。

ESCORT_ORDER = ['低', '低–中', '中', '中–高', '高']

# [班名, 大类, 主修方向, 最早起始年龄, 建议切入下限, 建议切入上限,
#  年费下限(万), 年费上限(万), 年费区间原文, 陪练强度, 陪练强度说明, 优势]
CLASSES = [
    ["少儿体适能", "体育运动类", "体育竞技", 3, 3, 6, 0.4, 1.5, "0.4–1.5", "低", "课后无强制练习，家庭配合运动习惯即可", "打牢跑跳、平衡、协调等通用运动底子，游戏化课堂孩子容易投入，为日后选专项打底，精力旺盛的低龄孩子尤其合适"],
    ["篮球", "体育运动类", "体育竞技", 5, 6, 8, 0.4, 1.2, "0.4–1.2", "低", "课后自主练球即可", "团队对抗中练协作、沟通与规则意识，跑跳投全面提升体能与协调，受众广、场地教练好找，是性价比高的团队运动入口"],
    ["足球", "体育运动类", "体育竞技", 5, 6, 8, 0.4, 1.2, "0.4–1.2", "低", "课后自主练球即可", "脚下控球与团队配合并重，大量奔跑释放精力、锻炼心肺与下肢力量，校园普及度高，容易衔接校内球队，孩子容易找到同伴一起玩"],
    ["羽毛球", "体育运动类", "体育竞技", 6, 7, 9, 0.6, 1.8, "0.6–1.8", "中", "需家长陪打或额外订场", "练手眼协调、反应速度与全场步伐，隔网对抗强度可控不易受伤，家长可以陪打，是全家都能参与、容易坚持的终身运动"],
    ["乒乓球", "体育运动类", "体育竞技", 5, 6, 8, 0.5, 1.5, "0.5–1.5", "中", "常需家长陪练或加课一对一对练", "小球高速对抗极练专注、反应与手眼协调，室内不受天气影响，单打双打都能练，陪练机会多，是适合长期坚持的技巧型运动"],
    ["网球", "体育运动类", "体育竞技", 5, 7, 9, 1.5, 5, "1.5–5.0（竞技型 8–20）", "中–高", "需约场地陪打，建议家长同练", "动作优雅、对抗全面的国际运动，分级球体系让孩子循序渐进，锻炼爆发力、协调与战术思维，掌握后是能陪伴一生的社交型运动"],
    ["游泳", "体育运动类", "体育竞技", 4, 5, 7, 0.6, 2, "0.6–2.0", "低", "课后可自主加练", "生存技能属性强，全身运动促进心肺发育和长高，水中沉浸感帮孩子释放精力、建立自信，掌握后是受益终身的技能"],
    ["跆拳道 / 空手道", "体育运动类", "体育竞技", 4, 5, 7, 0.3, 0.9, "0.3–0.9", "低", "课后可自主复习", "礼仪与品势打底，腿法拳法练柔韧、力量与身体控制，腰带晋级给孩子清晰的进阶感，防身技能与意志品质同步养成"],
    ["击剑", "体育运动类", "体育竞技", 5, 6, 8, 1.2, 2, "1.2–2.0（出成绩 6 万+）", "中", "需接送与赛事陪同", "一对一斗智斗勇，瞬间攻防练反应、胆量与战术决策，气质挺拔优雅，项目小众竞争压力小，是适合敢挑战孩子的对抗运动"],
    ["马术", "体育运动类", "体育竞技", 3, 7, 10, 1.5, 5, "1.5–5.0（竞技型 10–30）", "高", "赛事需全程陪同，差旅成本由家庭承担", "人马配合的独特运动，练平衡、核心与勇气，培养责任感与爱心，小众高端社交属性强，孩子与动物相处中学会耐心与共情"],
    ["轮滑 / 直排轮", "体育运动类", "体育竞技", 4, 5, 7, 0.3, 0.9, "0.3–0.9", "低", "课后可自主加练", "入门快、成就感来得早，练平衡、下肢力量与胆量，花式动作观赏性强，装备门槛低，是低龄孩子建立运动自信的第一站"],
    ["攀岩", "体育运动类", "体育竞技", 5, 7, 10, 0.8, 2.5, "0.8–2.5", "低–中", "可自主加练", "线路解谜式的向上挑战，练全身力量、协调与问题解决，每条线路都是新目标，登顶成就感直接，对胆量与专注是双重锻炼"],
    ["花样跳绳", "体育运动类", "体育竞技", 5, 6, 8, 0.2, 0.6, "0.2–0.6", "低", "可家庭自练", "成本极低的全身运动，练协调、节奏与心肺耐力，花样动作有挑战不枯燥，还是中考体育项目，练了直接为考试攒分"],
    ["滑板", "体育运动类", "体育竞技", 5, 6, 12, 0.5, 1.5, "0.5–1.5", "低–中", "课后可自主练，需关注场地安全", "街头风格运动，自由炫酷，练平衡、胆量与身体控制，每个新动作的突破都是自信的积累，男孩接受度高，户外社交属性也强"],
    ["钢琴", "音乐表演类", "艺术感知", 4, 5, 7, 0.8, 2.5, "0.8–2.5", "高", "低龄需全程陪练、记笔记、盯手型", "钢琴是乐器之王，体系最全，双手协调开发左右脑，识谱与视奏练专注和坚持，是音乐素养的通用底座，学好后再转任何乐器都快"],
    ["小提琴", "音乐表演类", "艺术感知", 5, 5, 7, 1, 3, "1.0–3.0", "高", "最难陪练，需长期陪听与纠姿", "旋律之王的歌唱性表达，练音准听觉、双手协调与身体姿态，便携且独奏重奏皆宜，坚持过入门期后音乐表达力极强"],
    ["古筝", "音乐表演类", "艺术感知", 5, 5, 7, 0.8, 2, "0.8–2.0", "中–高", "需监督日常练习", "民乐中入门最快，指法出声即好听，成就感来得早，练双手协调与音乐感觉，民族文化底蕴深厚，考级展演机会丰富"],
    ["二胡 / 琵琶 / 扬琴", "音乐表演类", "艺术感知", 6, 6, 8, 0.8, 2, "0.8–2.0", "中–高", "需监督日常练习", "传统民乐代表，音色独特感染力强，练听觉、双手精细配合与音乐表现力，师资相对稀缺，学好后就是难得的稀缺特长"],
    ["吉他（民谣/古典）", "音乐表演类", "艺术感知", 7, 8, 10, 0.7, 1.8, "0.7–1.8", "中", "可较快过渡到自主练习", "自弹自唱的快乐来得快，和弦与节奏练手指灵活与乐感，便携、成本低，是孩子展示自我的利器，也是终身的音乐陪伴"],
    ["尤克里里", "音乐表演类", "艺术感知", 5, 5, 7, 0.5, 1.2, "0.5–1.2", "中", "低龄需陪练，上手快", "最易上手的乐器，小巧轻便孩子抱得住，几节课就能弹唱简单歌曲，是建立音乐自信的理想起点，也是学吉他前的铺垫"],
    ["架子鼓", "音乐表演类", "艺术感知", 5, 6, 8, 0.8, 2, "0.8–2.0", "中", "需解决家庭噪音，建议在机构练", "节奏之王的律动感强，手脚协调练大脑分工与反应，宣泄精力、提升自信，孩子上手快成就感足，乐队合作机会多"],
    ["管乐（长笛/单簧管/萨克斯）", "音乐表演类", "艺术感知", 8, 9, 11, 1, 3, "1.0–3.0", "中", "需监督吹奏练习", "气息与指法并重，练肺活量、口型控制与手指灵活，乐团合奏培养团队协作，音色优美舞台表现力强，是学校乐团的主力声部"],
    ["声乐 / 童声", "音乐表演类", "艺术感知", 4, 6, 9, 0.6, 1.8, "0.6–1.8", "中", "需监督练声与曲目复习", "嗓子就是乐器零装备成本，练气息、咬字与音准，敢于当众演唱建立自信，舞台机会多，是最直接的音乐表达出口"],
    ["视唱练耳 / 音乐素养", "音乐表演类", "艺术感知", 5, 6, 8, 0.3, 0.9, "0.3–0.9", "低–中", "课后练习可自主完成", "听音、节奏、乐理的系统训练，是学任何乐器的底层能力，帮孩子听得懂、读得快、学得顺，是考级必备的基础课"],
    ["合唱团", "音乐表演类", "艺术感知", 6, 7, 10, 0.2, 0.6, "0.2–0.6", "低", "团队课，家长仅接送", "分声部合作中练音准、听觉与配合，团队归属感强、舞台演出多，成本最友好，是音乐启蒙与社交成长的双选路径"],
    ["中国舞", "舞蹈体态类", "艺术感知", 3, 4, 6, 0.4, 1.2, "0.4–1.2", "中–高", "需监督软开度与基本功练习", "身韵与柔韧并重，体态挺拔气质出挑，民族文化底蕴深，考级展演体系成熟，孩子上台机会多，是家长认知度最高的舞蹈入口"],
    ["芭蕾", "舞蹈体态类", "艺术感知", 3, 4, 6, 0.5, 1.5, "0.5–1.5", "中–高", "需监督基本功与体态", "体系最严谨的舞蹈，外开与姿态训练塑造挺拔体态与优雅气质，音乐感受力与身体控制同步提升，学芭蕾的孩子气质出众"],
    ["街舞（Hip-hop/Breaking）", "舞蹈体态类", "艺术感知", 5, 6, 9, 0.4, 1.2, "0.4–1.2", "低", "课后可自主练习", "律动感强自由酷炫，身体分离与地板技巧练协调和胆量，音乐节奏感提升明显，男孩接受度高，上台展示自信爆棚"],
    ["爵士舞", "舞蹈体态类", "艺术感知", 6, 7, 10, 0.4, 1.2, "0.4–1.2", "低", "课后可自主练习", "力量与线条并存的舞蹈，练身体控制、节奏感与舞台表现力，成品舞易出效果，孩子学起来有成就感，展示机会也很丰富"],
    ["拉丁舞", "舞蹈体态类", "艺术感知", 6, 7, 10, 0.6, 2.5, "0.6–2.5", "中–高", "赛事需服装、陪同与差旅", "热情奔放的双人舞，练节奏感、身体线条与舞伴配合的社交礼仪，五支舞风格各异不单调，赛场锻炼大方自信的表现力"],
    ["少儿模特 / 形体礼仪", "舞蹈体态类", "艺术感知", 4, 5, 8, 0.6, 3, "0.6–3.0", "中", "赛事 / 秀场需陪同，服装费高", "站姿步态与镜头感的系统训练，改善含胸驼背、提升自信与台风，礼仪修养同步养成，上台机会多，气质改变看得见"],
    ["啦啦操 / 健美操", "舞蹈体态类", "艺术感知", 6, 7, 10, 0.3, 0.9, "0.3–0.9", "低", "课后可自主复习", "团体项目活力四射，跳跃转体练体能与协调，口号与表现力练自信，校内衔接好、成本低，团队氛围让孩子爱上运动"],
    ["少儿形体矫正 / 体态训练", "舞蹈体态类", "艺术感知", 6, 7, 14, 0.6, 2, "0.6–2.0", "高", "家庭每日执行度决定效果", "针对含胸驼背、脊柱侧弯隐患的专项训练，核心力量与柔韧并重，管理日常姿态习惯，体态改善直接作用于形象与自信"],
    ["儿童创意美术", "美术创作类", "艺术感知", 3, 4, 6, 0.4, 1.2, "0.4–1.2", "低", "课后可自主涂鸦", "低龄美术首选，色彩与综合材料自由创作，保护想象力与表达欲，专注力与精细动作同步发展，每节课都有作品带回家"],
    ["素描 / 色彩（应试）", "美术创作类", "艺术感知", 9, 10, 12, 0.5, 1.5, "0.5–1.5", "中", "需保障长时段练习与画具补给", "造型与光影的系统训练，观察力、耐心与写实功底全面打牢，是美术专业路径的必修基础，考学作品集从这里起步"],
    ["国画", "美术创作类", "艺术感知", 6, 7, 9, 0.5, 1.3, "0.5–1.3", "低–中", "课后可自主练习", "笔墨之间练专注与静气，花鸟山水培养审美与文化认同，与书法天然搭配学习，传统文化底蕴深，考级展演体系成熟"],
    ["硬笔书法", "美术创作类", "艺术感知", 5, 6, 8, 0.3, 0.9, "0.3–0.9", "中", "需监督每日练字", "刚需型最强的兴趣班，坐姿握笔与间架结构直接改善作业卷面，练字即练专注与耐心，见效快，卷面分收益直接看得见"],
    ["毛笔书法", "美术创作类", "艺术感知", 6, 7, 9, 0.4, 1.1, "0.4–1.1", "中", "需监督日常临帖", "笔法结构章法的传统艺术，静心养性、培养审美与文化底蕴，与硬笔相辅相成，作品可参展可留藏，是能陪伴一生的爱好"],
    ["漫画 / 插画", "美术创作类", "艺术感知", 8, 9, 12, 0.5, 1.5, "0.5–1.5", "低", "课后可自主创作", "孩子最爱的画种，人物设定与分镜叙事兼有故事创作乐趣，兴趣黏性高易坚持，板绘方向可衔接数字创作，作品展示机会多"],
    ["陶艺", "美术创作类", "艺术感知", 5, 6, 9, 0.6, 1.6, "0.6–1.6", "低", "课后无强制练习", "泥巴的触感体验最治愈，手捏拉坯练精细动作与空间想象力，成品可带走成就感具体，是动手与艺术结合的放松型选择"],
    ["手工 / DIY", "美术创作类", "艺术感知", 3, 4, 6, 0.4, 1, "0.4–1.0", "低", "课后无强制练习", "剪贴拼装与立体造型，精细动作与专注力在玩乐中发展，成本最低的试水项目，能快速判断孩子动手与创作的兴趣倾向"],
    ["数字绘画 / 板绘 CG", "美术创作类", "艺术感知", 8, 9, 16, 0.5, 2, "0.5–2.0", "中", "需数位板与电脑环境支持", "衔接动漫与设计的数字创作，撤销重画降低挫败感，软件技法与美术功底并进，作品可发布可参赛，未来职业方向广"],
    ["播音主持 / 小主持人", "语言表达类", "语言沟通", 4, 5, 8, 0.5, 1.5, "0.5–1.5", "中", "需监督稿件练习", "正音、气息与舞台台风的系统训练，让孩子敢开口、会表达、上台稳，主持朗诵展演机会多，普通话水平与自信心同步提升"],
    ["戏剧表演 / 儿童剧", "语言表达类", "语言沟通", 5, 6, 9, 0.6, 1.8, "0.6–1.8", "低–中", "课后排练需家长配合接送", "解放天性与角色扮演中激活观察、想象与共情，语言表达和肢体表现同步提升，团队排演练协作，登台演出的成就感强烈"],
    ["演讲与口才", "语言表达类", "语言沟通", 6, 7, 10, 0.5, 1.5, "0.5–1.5", "中", "需家庭场景演练", "表达逻辑与即兴发言的专项训练，从敢说到说得好，台风肢体语言同步打磨，是课堂发言、班干部竞选的直接助力"],
    ["双语戏剧", "语言表达类", "语言沟通", 6, 7, 10, 1, 3, "1.0–3.0", "中", "需家庭英语环境配合", "英文剧本与角色扮演结合，语言在情境中自然输入输出，练敢开口的英文表达力，语言与戏剧双能力并进，演出成就感强"],
    ["朗诵", "语言表达类", "语言沟通", 6, 7, 10, 0.4, 1.1, "0.4–1.1", "中", "需监督日常朗读", "作品理解与情感表达并重，停连重音语调的处理练语感与气息，配乐朗诵的舞台呈现优美，是与播音主持互补的表达训练"],
    ["小记者 / 新闻采访", "语言表达类", "语言沟通", 8, 9, 12, 0.5, 1.4, "0.5–1.4", "中", "外出采访实践需陪同", "提问设计与实地采访的实践型课程，练沟通胆量、信息整理与文字写作，出镜报道体验独特，表达能力与写作能力双双提升"],
    ["辩论 / 演讲辩论", "语言表达类", "语言沟通", 8, 9, 16, 0.8, 2.5, "0.8–2.5", "中", "需关注辩题准备与赛事陪同", "立论驳论的思维交锋，练逻辑推理、论据检索与临场应变，辩题准备顺便拓展知识面，是强基面试关联度最高的训练"],
    ["围棋", "思维益智类", "理科思维", 4, 5, 7, 0.5, 2, "0.5–2.0", "中–高", "需监督网练与复盘", "思维训练性价比之王，计算、取舍与大局观在黑白之间养成，输赢挫折教育天然，段位进阶路径清晰，坐得住的孩子进步飞快"],
    ["中国象棋", "思维益智类", "理科思维", 5, 6, 8, 0.4, 1.2, "0.4–1.2", "中", "需监督日常对局", "成本最低的棋类启蒙，杀法与残局练计算和记忆，对局普及度高容易找到对手，传统文化底蕴深，随时随地能下一盘"],
    ["国际象棋", "思维益智类", "理科思维", 5, 6, 8, 0.5, 2, "0.5–2.0", "中", "需监督日常对局", "国际化程度最高的棋类，战术组合练计算与想象力，等级赛体系成熟，出国与国际学校家庭偏好，是全球通用的智力语言"],
    ["桥牌", "思维益智类", "理科思维", 9, 10, 13, 0.8, 2, "0.8–2.0", "中", "需搭档与日常练习配合", "叫牌与做庄的搭档博弈，概率计算与团队协作并重，智力门槛高、精英属性强，练逻辑推理与合作沟通，海外名校认可度高"],
    ["魔方", "思维益智类", "理科思维", 5, 6, 9, 0.3, 0.9, "0.3–0.9", "低–中", "可自主练习", "反馈最快的思维玩具，公式与手法练记忆、专注与手速，还原成功的成就感即时，成本低便携，是建立自信的速效项目"],
    ["珠心算", "思维益智类", "理科思维", 4, 5, 7, 0.4, 1.1, "0.4–1.1", "高", "需家庭每日打卡练习", "脑中打算盘的独特训练，计算速度与专注力提升明显，听算看算练瞬时记忆，计算能力收益确定，适合学龄前后的敏感期"],
    ["逻辑思维 / 数学思维（素养类）", "思维益智类", "理科思维", 5, 6, 8, 0.6, 1.8, "0.6–1.8", "中", "需配合课后练习", "找规律与推理游戏的课堂形式，思维启蒙与学科兴趣同步打底，不刷题重理解，为小学数学学习建立起底层自信心"],
    ["图形化编程 Scratch", "思维益智类", "理科思维", 6, 6, 8, 0.5, 1.8, "0.5–1.8", "中", "需家庭电脑 / 平板支持", "积木式编程零门槛入门，做游戏做动画的成就感强，事件循环条件这些编程概念在创作中理解，作品成果看得见摸得着"],
    ["Python 代码编程", "思维益智类", "理科思维", 8, 9, 11, 0.6, 2, "0.6–2.0", "中", "需家庭设备与练习监督", "真代码编程的入门语言，语法简洁上手快，小项目实战练逻辑与问题拆解，爬虫数据处理实用性强，衔接信息学方向"],
    ["C++ 信息学奥赛", "思维益智类", "理科思维", 10, 10, 12, 0.9, 3.2, "0.9–3.2", "高", "竞赛路线需长期投入与刷题陪跑", "信息学竞赛的官方语言，算法与数据结构训练强度大，竞赛成绩对小升初、强基计划含金量高，适合逻辑强的孩子深耕"],
    ["机器人（乐高/WeDo/EV3/Spike）", "思维益智类", "实验探究", 4, 6, 9, 0.6, 2, "0.6–2.0", "中", "课后搭建练习需设备与引导", "搭建与编程结合的动手课堂，结构与传感器知识在做项目中吸收，作品可参赛可展示，动手能力与编程思维双线成长"],
    ["科学实验", "思维益智类", "实验探究", 5, 6, 9, 0.6, 1.6, "0.6–1.6", "低–中", "课后可自主探索", "物理化学生物现象的动手探究，好奇心得到充分满足，实验设计与记录练科学思维，趣味性强，是最受欢迎的科普入口"],
    ["无人机", "思维益智类", "实验探究", 8, 9, 12, 1, 3, "1.0–3.0", "中", "需空旷场地练习与设备维护", "飞行操控与编程结合的新兴项目，组装调试练动手，穿越障碍练手眼协调，白名单赛事出口明确，科技特长路径清晰"],
    ["STEAM 综合", "思维益智类", "实验探究", 4, 5, 8, 0.8, 2.2, "0.8–2.2", "中", "视课程而定", "跨学科项目制学习，从提出问题到动手制作全流程体验，知识面广、综合能力锻炼全面，适合兴趣广泛的孩子探索"],
    ["AI 人工智能课", "思维益智类", "实验探究", 9, 10, 13, 0.6, 1.8, "0.6–1.8", "中–高", "需家庭设备与家长理解配合", "人工智能时代的启蒙课，理解数据与模型的基本概念、学会用好提示词，以项目产出为导向，是面向未来的必备素养课"],
    ["乐高大颗粒启蒙（3–6 岁）", "思维益智类", "实验探究", 3, 3, 6, 0.5, 1.5, "0.5–1.5", "低", "课后无强制练习", "大颗粒拼插与故事场景搭建，空间认知与手部精细动作在玩中发展，故事表达环节练开口表达，是编程与搭建的前置启蒙"],
    ["大语文 / 阅读写作", "文化素养类", "人文素养", 6, 7, 10, 0.6, 1.8, "0.6–1.8", "中", "需保障课外阅读量", "与校内语文强相关的刚需项目，精读与写作策略同步训练，文史常识积累厚实，阅读理解与作文分数的提升家长感知直接"],
    ["英语（口语/戏剧/自然拼读）", "文化素养类", "人文素养", 3, 4, 7, 0.8, 3, "0.8–3.0", "中", "需家庭语言环境配合", "语言敏感期的听说启蒙，自然拼读与情景口语打底，分级阅读体系循序渐进，外教互动课堂氛围好，发音语感赢在起点"],
    ["奥数 / 数学竞赛思维", "文化素养类", "人文素养", 7, 8, 10, 0.8, 2.5, "0.8–2.5", "高", "需长期刷题与竞赛陪跑", "数学思维的拔高训练，数论几何组合专题拓展解题深度，竞赛体系含金量高，适合学有余力的孩子，逻辑能力远超同龄人"],
    ["快速阅读 / 速读速记", "文化素养类", "人文素养", 7, 8, 11, 0.6, 1.8, "0.6–1.8", "中", "需配合家庭阅读训练", "视幅扩展与眼动训练提升阅读速度，信息提取与记忆法练知识吸收效率，是应对大量阅读任务的学习效率工具，效果可测可验证"],
    ["剑桥少儿英语 / KET·PET 冲刺", "文化素养类", "人文素养", 6, 7, 12, 0.6, 2, "0.6–2.0", "高", "需每日家庭听读与单词打卡", "剑桥体系的英语测评路径，词汇语法与听说读写系统训练，证书在小升初简历中有分量，备考目标明确、通过率可查证"],
    ["专注力 / 学习能力训练", "文化素养类", "人文素养", 5, 6, 10, 0.8, 3, "0.8–3.0", "高", "家庭执行度决定效果", "视觉听觉注意与工作记忆的专项训练，听课与作业习惯同步养成，是学习能力的底层修复工程，为所有学科学习提效"],
    ["厨艺 / 烘焙", "生活素养与其它", "", 4, 5, 9, 0.6, 1.6, "0.6–1.6", "低–中", "课后可在家复做", "动手做美食的沉浸体验，精细动作与流程执行在灶台边练成，营养与餐桌礼仪同步养成，孩子接受度最高、全家受益"],
    ["财商教育", "生活素养与其它", "", 6, 7, 11, 0.6, 1.6, "0.6–1.6", "中", "需家庭场景配合，如零花钱管理", "零花钱与记账的真实场景教学，需要与想要、储蓄与风险的概念早建立，数学应用与消费决策同步提升，是受益终身的素养"],
    ["摄影", "生活素养与其它", "", 8, 9, 13, 0.6, 1.8, "0.6–1.8", "中", "需设备投入与外拍陪同", "观察力与审美的随身训练，构图用光在实拍中体会，作品即成果反馈快，外出拍摄顺便亲子同行，是门槛低的艺术表达"],
    ["营地教育 / 户外生存", "生活素养与其它", "", 6, 7, 16, 0.3, 2, "0.3–2.0", "低", "营地期间由机构全程看护", "露营绳结与野外技能的团队挑战，独立性与勇气在真实场景中锻炼，远离屏幕拥抱自然，师资配比与安全预案是挑选关键"],
]

# 口语/类型库写法 -> 速查表标准班名（文本匹配用；泛化词已剔除）
ALIASES = {
    "AI 课": "AI 人工智能课",
    "Breaking": "街舞（Hip-hop/Breaking）",
    "C++": "C++ 信息学奥赛",
    "CG": "数字绘画 / 板绘 CG",
    "DIY": "手工 / DIY",
    "EV3": "机器人（乐高/WeDo/EV3/Spike）",
    "Hip-hop": "街舞（Hip-hop/Breaking）",
    "KET": "剑桥少儿英语 / KET·PET 冲刺",
    "PET": "剑桥少儿英语 / KET·PET 冲刺",
    "Python": "Python 代码编程",
    "STEAM": "STEAM 综合",
    "Scratch": "图形化编程 Scratch",
    "Spike": "机器人（乐高/WeDo/EV3/Spike）",
    "WeDo": "机器人（乐高/WeDo/EV3/Spike）",
    "下围棋": "围棋",
    "下象棋": "中国象棋",
    "专注力": "专注力 / 学习能力训练",
    "中国舞": "中国舞",
    "中国象棋": "中国象棋",
    "主持": "播音主持 / 小主持人",
    "举重": "大重量杠铃 / 举重 / 长期静力（倒立、扎马步、负重深蹲）",
    "乌克丽丽": "尤克里里",
    "乐理": "视唱练耳 / 音乐素养",
    "乐高": "乐高大颗粒启蒙（3–6 岁）",
    "乐高搭建": "乐高大颗粒启蒙（3–6 岁）",
    "乐高机器人": "机器人（乐高/WeDo/EV3/Spike）",
    "乒乓球班": "乒乓球",
    "书法": "硬笔书法",
    "二胡": "二胡 / 琵琶 / 扬琴",
    "人工智能": "AI 人工智能课",
    "代码编程": "Python 代码编程",
    "体态": "少儿形体矫正 / 体态训练",
    "体操": "体操 / 艺术体操 / 蹦床 / 跑酷 / 技巧翻腾",
    "体能课": "少儿体适能",
    "体适能": "少儿体适能",
    "作文": "大语文 / 阅读写作",
    "信奥": "C++ 信息学奥赛",
    "信息学": "C++ 信息学奥赛",
    "做饭课": "厨艺 / 烘焙",
    "健美操": "啦啦操 / 健美操",
    "儿童剧": "戏剧表演 / 儿童剧",
    "写作": "大语文 / 阅读写作",
    "写字课": "硬笔书法",
    "击剑班": "击剑",
    "创意美术": "儿童创意美术",
    "剑桥少儿": "剑桥少儿英语 / KET·PET 冲刺",
    "剑桥英语": "剑桥少儿英语 / KET·PET 冲刺",
    "剑道": "击剑",
    "力量训练": "力量 / 抗阻训练（自重、弹力带、低负荷器械）",
    "动漫": "漫画 / 插画",
    "单簧管": "管乐（长笛/单簧管/萨克斯）",
    "厨艺": "厨艺 / 烘焙",
    "双语戏剧": "双语戏剧",
    "口才主持": "播音主持 / 小主持人",
    "口才班": "播音主持 / 小主持人",
    "古典吉他": "吉他（民谣/古典）",
    "古筝班": "古筝",
    "合唱": "合唱团",
    "合唱团": "合唱团",
    "吉他": "吉他（民谣/古典）",
    "唱歌班": "声乐 / 童声",
    "唱诗班": "合唱团",
    "啦啦操": "啦啦操 / 健美操",
    "围棋": "围棋",
    "国标": "拉丁舞",
    "国球": "乒乓球",
    "国画": "国画",
    "国象": "国际象棋",
    "国际象棋": "国际象棋",
    "图形化编程": "图形化编程 Scratch",
    "声乐": "声乐 / 童声",
    "大提琴": "大提琴",
    "大语文": "大语文 / 阅读写作",
    "大语文素养": "大语文 / 阅读写作",
    "大颗粒": "乐高大颗粒启蒙（3–6 岁）",
    "奥数": "奥数 / 数学竞赛思维",
    "学习能力": "专注力 / 学习能力训练",
    "学游泳": "游泳",
    "学琴": "钢琴",
    "学骑马": "马术",
    "实验班": "科学实验",
    "小主持人": "播音主持 / 小主持人",
    "小提琴": "小提琴",
    "小记者": "小记者 / 新闻采访",
    "少儿编程": "图形化编程 Scratch",
    "尤克里里": "尤克里里",
    "应试美术": "素描 / 色彩（应试）",
    "弹古筝": "古筝",
    "弹吉他": "吉他（民谣/古典）",
    "弹钢琴": "钢琴",
    "形体矫正": "少儿形体矫正 / 体态训练",
    "形体礼仪": "少儿模特 / 形体礼仪",
    "心算": "珠心算",
    "快速阅读": "快速阅读 / 速读速记",
    "思维课": "逻辑思维 / 数学思维（素养类）",
    "感统体能": "少儿体适能",
    "戏剧": "戏剧表演 / 儿童剧",
    "戏剧表演": "戏剧表演 / 儿童剧",
    "户外": "营地教育 / 户外生存",
    "手工": "手工 / DIY",
    "手工课": "手工 / DIY",
    "扎马步": "大重量杠铃 / 举重 / 长期静力（倒立、扎马步、负重深蹲）",
    "打篮球": "篮球",
    "打鼓": "架子鼓",
    "扬琴": "二胡 / 琵琶 / 扬琴",
    "技巧翻腾": "体操 / 艺术体操 / 蹦床 / 跑酷 / 技巧翻腾",
    "抗阻训练": "力量 / 抗阻训练（自重、弹力带、低负荷器械）",
    "拉丁": "拉丁舞",
    "拉丁舞": "拉丁舞",
    "拉坯": "陶艺",
    "拉琴": "小提琴",
    "拍照": "摄影",
    "捏泥": "陶艺",
    "插画": "漫画 / 插画",
    "摄影": "摄影",
    "播音": "播音主持 / 小主持人",
    "攀岩课": "攀岩",
    "数字绘画": "数字绘画 / 板绘 CG",
    "数学思维": "逻辑思维 / 数学思维（素养类）",
    "数学竞赛": "奥数 / 数学竞赛思维",
    "无人机": "无人机",
    "旱冰": "轮滑 / 直排轮",
    "朗诵": "朗诵",
    "朗诵班": "朗诵",
    "机器人": "机器人（乐高/WeDo/EV3/Spike）",
    "杠铃": "大重量杠铃 / 举重 / 长期静力（倒立、扎马步、负重深蹲）",
    "板绘": "数字绘画 / 板绘 CG",
    "架子鼓": "架子鼓",
    "桥牌": "桥牌",
    "模特": "少儿模特 / 形体礼仪",
    "武术班": "跆拳道 / 空手道",
    "毛笔": "毛笔书法",
    "民族舞": "中国舞",
    "民舞": "中国舞",
    "水墨画": "国画",
    "注意力训练": "专注力 / 学习能力训练",
    "泳课": "游泳",
    "游泳班": "游泳",
    "滑板班": "滑板",
    "演讲": "演讲与口才",
    "演讲班": "演讲与口才",
    "漫画": "漫画 / 插画",
    "烘焙": "厨艺 / 烘焙",
    "爬墙": "攀岩",
    "爵士舞": "爵士舞",
    "爵士鼓": "架子鼓",
    "玩滑板": "滑板",
    "玩魔方": "魔方",
    "珠心算": "珠心算",
    "理财课": "财商教育",
    "琴课": "钢琴",
    "琵琶": "二胡 / 琵琶 / 扬琴",
    "画画": "儿童创意美术",
    "直排轮": "轮滑 / 直排轮",
    "硬笔": "硬笔书法",
    "科学实验": "科学实验",
    "科学课": "科学实验",
    "积木课": "乐高大颗粒启蒙（3–6 岁）",
    "空手道": "跆拳道 / 空手道",
    "童声": "声乐 / 童声",
    "管乐": "管乐（长笛/单簧管/萨克斯）",
    "篮球班": "篮球",
    "素描": "素描 / 色彩（应试）",
    "练字": "硬笔书法",
    "编程竞赛": "C++ 信息学奥赛",
    "网球班": "网球",
    "美术启蒙": "儿童创意美术",
    "羽毛球班": "羽毛球",
    "耐力跑": "长距离耐力跑 / 负重徒步",
    "自然拼读": "英语（口语/戏剧/自然拼读）",
    "航模": "无人机",
    "色彩": "素描 / 色彩（应试）",
    "艺术体操": "体操 / 艺术体操 / 蹦床 / 跑酷 / 技巧翻腾",
    "芭蕾": "芭蕾",
    "芭蕾舞": "芭蕾",
    "花样跳绳班": "花样跳绳",
    "英语启蒙": "英语（口语/戏剧/自然拼读）",
    "英语戏剧": "双语戏剧",
    "英语班": "英语（口语/戏剧/自然拼读）",
    "营地": "营地教育 / 户外生存",
    "萨克斯": "管乐（长笛/单簧管/萨克斯）",
    "街舞": "街舞（Hip-hop/Breaking）",
    "表演班": "戏剧表演 / 儿童剧",
    "视唱练耳": "视唱练耳 / 音乐素养",
    "象棋": "中国象棋",
    "负重徒步": "长距离耐力跑 / 负重徒步",
    "负重深蹲": "大重量杠铃 / 举重 / 长期静力（倒立、扎马步、负重深蹲）",
    "财商": "财商教育",
    "走秀": "少儿模特 / 形体礼仪",
    "足球班": "足球",
    "跆拳道": "跆拳道 / 空手道",
    "跑酷": "体操 / 艺术体操 / 蹦床 / 跑酷 / 技巧翻腾",
    "跨学科": "STEAM 综合",
    "跳绳": "花样跳绳",
    "踢足球": "足球",
    "蹦床": "体操 / 艺术体操 / 蹦床 / 跑酷 / 技巧翻腾",
    "轮滑": "轮滑 / 直排轮",
    "软笔书法": "毛笔书法",
    "辩论": "辩论 / 演讲辩论",
    "辩论班": "辩论 / 演讲辩论",
    "速读": "快速阅读 / 速读速记",
    "速读速记": "快速阅读 / 速读速记",
    "逻辑思维": "逻辑思维 / 数学思维（素养类）",
    "采访班": "小记者 / 新闻采访",
    "野外生存": "营地教育 / 户外生存",
    "长笛": "管乐（长笛/单簧管/萨克斯）",
    "长跑": "长距离耐力跑 / 负重徒步",
    "阅读写作": "大语文 / 阅读写作",
    "陶艺": "陶艺",
    "音乐素养": "视唱练耳 / 音乐素养",
    "项目制": "STEAM 综合",
    "马术课": "马术",
    "驼背矫正": "少儿形体矫正 / 体态训练",
    "骑马": "马术",
    "魔方": "魔方",
}

# 硬红线（references_05 第八节，取严后口径）：{班名: [生效年龄, 安全提示]}
HARD_LINES = {
    "架子鼓": [6, "风险极高（噪声性聋不可逆）：降噪耳罩（≥20 dB）+ 单次 ≤30–60 分钟每日 ≤1 小时 + 优先电子鼓 + 每年纯音测听"],
    "吉他（民谣/古典）": [8, "手指按弦初期会痛；须按身高调整琴箱尺寸与持琴角度"],
    "拉丁舞": [6, "小学阶段不进竞技套路（膝关节扭转负荷，已有 9 岁女童练 5 年致半月板撕裂病例）；比赛服与赛事费用是隐性大头"],
    "街舞（Hip-hop/Breaking）": [5, "8 岁前不做地板动作/倒立/头转（腕肘肩扭伤与骨折高发）；技巧训练须地垫、护腕与教练一对一保护，禁止在家自练"],
    "中国舞": [3, "10 岁前禁止下腰、空翻（无骨折脱位型脊髓损伤，已成儿童脊髓损伤第一位原因）；8 岁前禁止硬性压腿/外力开肩开胯"],
    "芭蕾": [3, "足尖鞋 12 岁前不建议（10–11 岁绝对下限且须 2–3 年系统训练+教师评估双条件）；每次足尖训练 ≤15–20 分钟；男孩不穿足尖鞋"],
    "少儿模特 / 形体礼仪": [4, "12 岁前不进任何\"塑形、控重、束身\"类训练；出现\"硬性达标、体重管控、束身工具、忍痛过关\"应立刻退出"],
    "少儿形体矫正 / 体态训练": [6, "8 岁前禁止硬性压腿外力加压；疑似脊柱侧弯须先就医，培训不能替代医疗"],
    "轮滑 / 直排轮": [4, "已有 X 型腿/佝偻病者不推荐；硬壳鞋+头盔+护腕护肘护膝齐全；单次 5–10 分钟即休息"],
    "羽毛球": [6, "持拍侧单侧发力→左右肌力失衡，须配合弱势侧训练"],
    "网球": [5, "6 岁前避免大量拉伸与跨步训练；持拍侧单侧发力须配合弱势侧训练"],
    "篮球": [5, "小学低年级以拍球、投篮趣味形式参与，高强度对抗与弹跳训练放到小学中高年级之后"],
    "足球": [5, "低龄段头部冲顶训练应推迟"],
    "花样跳绳": [5, "严禁突击加量（胫骨结节骨骺炎，膝盖下方固定痛点+鼓包必须停训就医）；前脚掌落地、减震鞋、禁水泥地"],
    "游泳": [4, "学会≠会自救，家长不可因此放松看护；注意场馆水质与教练救生资质"],
    "攀岩": [5, "严禁 full crimp 全屈指握法、指力板、连续高强度抱石（手指生长板应力性损伤）；每周至少 2 天休息日；指根持续隐痛立即停攀就医"],
    "乒乓球": [5, "无额外强制提示"],
    "跆拳道 / 空手道": [4, "无额外强制提示；注意考级频率是否被滥用（速查表备注）"],
    "马术": [3, "无额外强制提示；注意场馆安全规范"],
    "击剑": [5, "无额外强制提示"],
    "滑板": [5, "护具必须齐全；场地（碗池/滑板公园）决定上限"],
    "小提琴": [5, "12 岁以下用 1/2 或 3/4 尺寸琴；每练 20–40 分钟休息+5–10 分钟反向拉伸；每年评估脊柱体态"],
    "钢琴": [4, "4–5 岁接受技术指导较适宜；控制单次时长（≤20–30 分钟）与近视负荷"],
    "古筝": [5, "遵循跨乐器三条硬规则（时长分段/反向补偿/每年体检）"],
    "二胡 / 琵琶 / 扬琴": [6, "遵循跨乐器三条硬规则"],
    "管乐（长笛/单簧管/萨克斯）": [8, "须等换牙与肺活量达标；正畸期间须暂停或缩短铜管练习"],
    "声乐 / 童声": [4, "变声期（男童 12–14 岁）需重新评估（速查表备注）"],
}

# 风险资料独有班（速查表外，无费用/陪练/优势数据）：[班名, 生效年龄, 要点]
RISK_EXTRA = [
    ["大提琴", 6, "6 岁前不建议（肩宽不足）；6–8 岁起，琴体大 8 岁后才能舒适合抱；不对称姿态风险同小提琴，遵循跨乐器三条硬规则"],
    ["体操 / 艺术体操 / 蹦床 / 跑酷 / 技巧翻腾", 6, "6 岁前不建议系统训练；3–6 岁以体能、协调、动作模式为主；临床常见半月板韧带三联损伤；已有 6 岁儿童练跑酷致小腿骨折病例"],
    ["力量 / 抗阻训练（自重、弹力带、低负荷器械）", 7, "力量训练影响长高无科学证据；7–8 岁即可开始，每周 2–3 次、低负荷多次数、须有人指导；禁大重量、憋气、爆发性动作"],
    ["大重量杠铃 / 举重 / 长期静力（倒立、扎马步、负重深蹲）", 12, "小学阶段禁止：静力压迫可使参与身高发育的软骨提前钙化；憋气有碍静脉血回流与心肺发育"],
    ["长距离耐力跑 / 负重徒步", 6, "6 岁前单次 2000 米以上不建议；6–12 岁 2 小时以上徒步不建议；低龄以短距离、间歇性、游戏化为主"],
]

# ==== GENERATED DATA END ====

CLASS_BY_NAME = {c[0]: c for c in CLASSES}
RISK_BY_NAME = {r[0]: r for r in RISK_EXTRA}

# 类型库（references_03 v1.0.6 全量补充）收录但速查表未收录的班：无年龄/费用/陪练/优势数据，
# 返回 in_speed_table=false 条目而非 classes_not_found；"（低龄段）"后缀归一到裸名。
OFFTABLE_CLASSES = {
    "中考体育集训": "体育竞技", "亲子游泳": "体育竞技",
    "美术艺考": "艺术感知", "传媒艺考": "艺术感知",
    "音乐艺考": "艺术感知", "奥尔夫音乐启蒙": "艺术感知",
    "亲子绘本阅读": "人文素养",
}

CLASS_KEYS = {"classes", "class_names", "interest_classes", "班级", "兴趣班"}
AGE_KEYS = {"age", "年龄", "child_age", "孩子年龄"}
GRADE_KEYS = {"grade", "年级", "child_grade"}
BUDGET_KEYS = {"budget", "预算", "年预算"}
DIRECTION_KEYS = {"directions", "direction", "t11_directions", "主修方向", "方向"}
# v1.2.7：类别参数（十六测实测 planner 传 category:"艺术类"，旧版不认 → classes_not_found
# → 模型凭记忆组装推荐、年龄窗口规则整体失效）；口径与速查表「大类」列一致
CATEGORY_KEYS = {"category", "categories", "类别", "大类", "类别名"}
TEXT_KEYS = {"query", "user_input", "text", "input", "content", "message", "question"}
MSG_LIST_KEYS = {"messages", "history", "conversation"}

GRADE_AGE = {"一年级": 6, "二年级": 7, "三年级": 8, "四年级": 9, "五年级": 10, "六年级": 11,
             "1年级": 6, "2年级": 7, "3年级": 8, "4年级": 9, "5年级": 10, "6年级": 11,
             "初一": 12, "七年级": 12, "7年级": 12, "初二": 13, "八年级": 13, "8年级": 13,
             "初三": 14, "九年级": 14, "9年级": 14,
             "学前班": 5, "大班": 5, "中班": 4, "小班": 3, "幼儿园": 3}

VALID_DIRECTIONS = sorted({c[2] for c in CLASSES if c[2]})

# v1.2.7 类别体系：速查表「大类」全集 + 用户/模型常用类别词别名（去"类"后缀归一）
CATEGORY_NAMES = sorted({c[1] for c in CLASSES if c[1]})
CATEGORY_BARE = {(c[:-1] if c.endswith("类") else c): c for c in CATEGORY_NAMES}
CATEGORY_ALIASES = {
    "艺术": ["音乐表演", "舞蹈体态", "美术创作"],
    "表演": ["音乐表演", "舞蹈体态", "语言表达"], "舞台": ["音乐表演", "舞蹈体态", "语言表达"],
    "登台": ["音乐表演", "舞蹈体态", "语言表达"],
    "音乐": ["音乐表演"], "乐器": ["音乐表演"], "声乐": ["音乐表演"],
    "舞蹈": ["舞蹈体态"],
    "美术": ["美术创作"], "绘画": ["美术创作"], "画画": ["美术创作"],
    "体育": ["体育运动"], "运动": ["体育运动"],
    "语言": ["语言表达"], "口才": ["语言表达"], "表达": ["语言表达"],
    "棋": ["思维益智"], "思维": ["思维益智"], "益智": ["思维益智"],
    "编程": ["思维益智"], "科技": ["思维益智"],
    "文化": ["文化素养"], "素养": ["文化素养"], "语文": ["文化素养"],
    "英语": ["文化素养"],
    "生活": ["生活素养与其它"], "其它": ["生活素养与其它"],
    "其他": ["生活素养与其它"],
}

ESCORT_RANK = {v: i for i, v in enumerate(ESCORT_ORDER)}

OUTPUT_REMINDERS = [
    "任何输出涉及年费/预算时，必须附：以上价格为参考区间，不同地区价格差异较大，请以您家附近机构的实际报价为准。",
    "给出推荐或体验课建议时，必须附：请确认您家附近有开设此兴趣班，以便您带着孩子报名学习。",
]


def _walk(obj, depth=0):
    """产出 (key, value)，value 可为任意类型（标量/列表/字典）；
    跳过 assistant/system 消息，深度 ≤4。"""
    if depth > 4 or obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            if isinstance(v, (dict, list)):
                yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item.get("role") in ("assistant", "system", "bot"):
                continue
            if isinstance(item, (dict, list)):
                yield from _walk(item, depth + 1)


def _parse_age_value(v):
    """数字或 '7岁'/'7周岁' → int（向下取整，保守取小）；无数字返回 None。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v) if v >= 0 else None
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else None


def _parse_grade_value(v):
    """年级值 → (标准化年级名, 对应年龄)；中文/阿拉伯数字年级均可；无匹配返回 (None, None)。"""
    s = re.sub(r"\s+", "", str(v))
    for g in sorted(GRADE_AGE, key=len, reverse=True):
        if g in s:
            return g, GRADE_AGE[g]
    return None, None


def _budget_from_text(text):
    if not text:
        return None
    cands = []  # (pos, value万)
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[wW万]", text):
        cands.append((m.start(1), float(m.group(1))))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK千]", text):
        cands.append((m.start(1), float(m.group(1)) / 10))
    kw = [m.start() for m in re.finditer(r"预算|花费|花销|一年|每年|价位|报价|学费", text)]
    if kw:
        for m in re.finditer(r"(\d{4,6})(?!\d)(?!年)", text):
            cands.append((m.start(1), float(m.group(1)) / 10000))
    if not cands:
        return None
    if kw:
        best = min(cands, key=lambda c: min(abs(c[0] - k) for k in kw))
        return round(best[1], 2)
    return round(cands[0][1], 2)


def _parse_budget_value(v):
    """数字或文本 → 万元；数字 ≥1000 视为元自动换算（10000 → 1.0），<1000 视为万元。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = float(v)
        if n < 0:
            return None
        if n >= 1000:
            n /= 10000
        return round(n, 2)
    s = str(v).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return _parse_budget_value(float(s))
    return _budget_from_text(s)


def _match_names_in_text(text):
    """文本中匹配班名/别名：候选长词优先，重叠区间去重。"""
    if not text:
        return []
    candidates = sorted(set(list(ALIASES) + list(CLASS_BY_NAME) + list(RISK_BY_NAME)),
                        key=len, reverse=True)
    spans, found = [], []
    for key in candidates:
        for m in re.finditer(re.escape(key), text, re.IGNORECASE):
            s, e = m.span()
            if any(not (e <= s0 or s >= e0) for s0, e0 in spans):
                continue
            spans.append((s, e))
            target = ALIASES.get(key, key)
            found.append((s, key, target))
    found.sort(key=lambda x: x[0])
    return found


def _resolve_query(q):
    """单个班名查询 → (标准班名 / 风险独有班名 / 表外班名 或 None)。"""
    q = str(q).strip()
    for cand in (q, re.sub(r"\s+", "", q), re.sub(r"班$", "", q),
                 re.sub(r"（低龄段）$|\(低龄段\)$", "", q)):
        if not cand:
            continue
        if cand in ALIASES:
            return ALIASES[cand]
        if cand in CLASS_BY_NAME or cand in RISK_BY_NAME or cand in OFFTABLE_CLASSES:
            return cand
    return None


def _fmt_num(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _norm_direction(q):
    """方向名归一：剥"主修/主攻"前缀与"方向/赛道"后缀（十一测-2 实测：模型把
    t11_directions 写成"艺术感知方向"等自然写法，原样匹配失败 → ok:false）。"""
    q = re.sub(r"\s+", "", str(q))
    for pre in ("主修", "主攻"):
        if q.startswith(pre) and len(q) > len(pre):
            q = q[len(pre):]
            break
    for suf in ("方向", "赛道"):
        if q.endswith(suf) and len(q) > len(suf):
            q = q[: -len(suf)]
            break
    return q


def _class_result(query, std_name, age, budget):
    row = CLASS_BY_NAME[std_name]
    name, cat, direction, raw_floor, rec_lo, rec_hi, fee_lo, fee_hi, fee_raw, escort, escort_note, adv = row
    hard = HARD_LINES.get(std_name)
    risk_floor = hard[0] if hard else None
    floor = max(raw_floor, risk_floor) if risk_floor else raw_floor

    if age is None:
        age_ok, status = None, "age_unknown"
        note = f"最早{floor}岁可开始，建议{rec_lo}–{rec_hi}岁切入，请对照孩子年龄确认"
    elif age < floor:
        age_ok, status = False, "below_floor"
        if risk_floor and risk_floor > raw_floor:
            note = (f"速查表最早{raw_floor}岁，但按身体风险口径建议{risk_floor}岁后开始；"
                    f"孩子{age}岁还未到")
        else:
            note = f"最早{floor}岁可开始；孩子{age}岁还未到"
    elif age < rec_lo:
        age_ok, status = True, "below_recommended"
        note = f"已到最早起始年龄（{floor}岁），建议{rec_lo}–{rec_hi}岁切入，现在可以以体验为主"
    elif age > rec_hi:
        age_ok, status = True, "past_recommended"
        note = (f"孩子{age}岁已过建议切入区间（{rec_lo}–{rec_hi}岁），课程内容偏基础启蒙向，"
                "默认不作为新推荐；窗口内与未到下限的班不足时可补位并附固定句")
    else:
        age_ok, status = True, "ok"
        note = f"年龄条件满足（最早{floor}岁，建议{rec_lo}–{rec_hi}岁切入）"

    fee_disp = re.sub(r"（[^）]*）", "", fee_raw)
    if budget is None:
        budget_sufficient = None
        budget_note = "预算未提供，按年费区间列出"
    elif budget < fee_lo:
        budget_sufficient = False
        budget_note = (f"{name}年费约{fee_disp}万元，可能超出您{_fmt_num(budget)}万元的预算，"
                       f"报名前请留意")
    else:
        budget_sufficient = True
        budget_note = f"年费约{fee_disp}万元，在您{_fmt_num(budget)}万元的预算内"

    return {
        "query": query, "name": name, "matched": True, "in_speed_table": True,
        "category": cat, "direction": direction,
        "age_floor": floor, "speed_table_age": raw_floor, "risk_age": risk_floor,
        "recommended_age": [rec_lo, rec_hi],
        "age_ok": age_ok, "age_status": status, "age_note": note,
        "fee_range": fee_raw, "fee_low": fee_lo, "fee_high": fee_hi,
        "budget_sufficient": budget_sufficient, "budget_note": budget_note,
        "escort": escort, "escort_rank": ESCORT_RANK[escort], "escort_note": escort_note,
        "advantage": adv,
        "risk_note": hard[1] if hard else None,
    }


def _risk_extra_result(query, risk_name, age):
    _name, floor, note = RISK_BY_NAME[risk_name]
    if age is None:
        age_ok, status = None, "age_unknown"
        age_note = f"建议{floor}岁后开始，请对照孩子年龄确认"
    elif age < floor:
        age_ok, status = False, "below_floor"
        age_note = f"该项目建议{floor}岁后开始；孩子{age}岁还未到。{note}"
    else:
        age_ok, status = True, "ok"
        age_note = f"年龄条件满足（建议{floor}岁后开始）。{note}"
    return {
        "query": query, "name": risk_name, "matched": True, "in_speed_table": False,
        "category": "风险资料独有（速查表外）", "direction": None,
        "age_floor": floor, "speed_table_age": None, "risk_age": floor,
        "recommended_age": None,
        "age_ok": age_ok, "age_status": status, "age_note": age_note,
        "fee_range": None, "fee_low": None, "fee_high": None,
        "budget_sufficient": None,
        "budget_note": "该项目未收录在精选指导表中，费用与课程安排请以当地机构为准",
        "escort": None, "escort_rank": None, "escort_note": None,
        "advantage": None,
        "risk_note": note,
    }


def _offtable_result(query, name):
    """类型库收录但速查表未收录的班 → in_speed_table=false 条目（无数据，禁止编造）。"""
    return {
        "query": query, "name": name, "matched": True, "in_speed_table": False,
        "category": "类型库收录（速查表外）", "direction": OFFTABLE_CLASSES[name],
        "age_floor": None, "speed_table_age": None, "risk_age": None,
        "recommended_age": None,
        "age_ok": None, "age_status": "off_table",
        "age_note": "该项目未收录在精选指导表中，适学年龄请以当地机构为准",
        "fee_range": None, "fee_low": None, "fee_high": None,
        "budget_sufficient": None,
        "budget_note": "该项目未收录在精选指导表中，费用与课程安排请以当地机构为准",
        "escort": None, "escort_rank": None, "escort_note": None,
        "advantage": None, "risk_note": None,
    }


def _resolve_category(q):
    """类别词 → 大类列表（v1.2.7）：精确大类名 / 去"类"后缀 / 常用别名（艺术→三目）。
    无匹配返回 None。仅确定性映射，不做模糊猜测。"""
    s = re.sub(r"\s+", "", str(q))
    if not s:
        return None
    t = s[:-1] if s.endswith("类") and len(s) > 1 else s
    if t in CATEGORY_BARE:
        return [CATEGORY_BARE[t]]
    if t in CATEGORY_ALIASES:
        cats = [CATEGORY_BARE[a] for a in CATEGORY_ALIASES[t] if a in CATEGORY_BARE]
        return cats or None
    return None


def _pool_sorted(rows, age, budget=None):
    """班池排序（references_08 第六节 v1.2.12 四级阶梯）：
    第一级 年龄满足（窗口内 > 已到最早起始未到下限）且预算充足 >
    第二级 过窗（past_recommended）且预算充足 >
    第三级 年龄满足但预算不足 >
    第四级 过窗且预算不足（殿后，默认不推荐）> 未到最早起始/年龄未知；
    级内：窗口内 > 未到下限，陪练低在前，年费下限低在前。预算未提供按充足处理。"""
    def _fit(row):
        hard = HARD_LINES.get(row[0])
        floor = max(row[3], hard[0]) if hard else row[3]
        if age is None:
            return 2
        if age < floor:
            return 3
        if row[4] <= age <= row[5]:
            return 0
        return 2 if age > row[5] else 1

    def _tier(row):
        fit = _fit(row)
        if fit == 3:
            return 4
        b_ok = True if budget is None else budget >= row[6]
        if fit <= 1:
            return 0 if b_ok else 2
        return 1 if b_ok else 3

    return sorted(rows, key=lambda r: (_tier(r), _fit(r), ESCORT_RANK[r[9]], r[6]))


_POOL_SORT_RULE = ("四级阶梯（v1.2.12，references_08 第六节）：第一级 年龄满足（建议切入区间内 > "
                   "已到最早起始未到切入下限）且预算充足 > 第二级 已过建议切入区间（past_recommended，"
                   "默认不推荐仅补位）且预算充足 > 第三级 年龄满足但预算不足 > 第四级 过窗且预算不足"
                   "（殿后，默认不推荐）> 未到最早起始/年龄未知；级内陪练强度低在前 > 年费下限低在前；"
                   "预算未提供按充足处理；全部班照常返回，由调用方按四级阶梯与'不足 3 个才降级'规则处理")


def _direction_package(direction, age, budget):
    """方向 → 全班池数据包（references_08 第六节 v1.2.12 四级阶梯排序）。"""
    rows = _pool_sorted([c for c in CLASSES if c[2] == direction], age, budget)
    return {
        "direction": direction,
        "sort_rule": _POOL_SORT_RULE,
        "classes": [_class_result(r[0], r[0], age, budget) for r in rows],
    }


def _category_package(query, cats, age, budget):
    """类别 → 全班池数据包（v1.2.7）：按「大类」列展开（艺术类→音乐表演/舞蹈体态/
    美术创作三目全部班），排序与方向包同规则，年龄/预算/陪练全套处理照常。"""
    rows = _pool_sorted([c for c in CLASSES if c[1] in cats], age, budget)
    return {
        "category_query": query,
        "expanded_categories": cats,
        "class_count": len(rows),
        "sort_rule": _POOL_SORT_RULE,
        "classes": [_class_result(r[0], r[0], age, budget) for r in rows],
    }


def main(payload):
    if not isinstance(payload, dict):
        payload = {}

    age = age_source = None
    grade = None
    grade_raw = None
    budget = None
    class_queries = []
    direction_queries = []
    category_queries = []
    texts = []

    for k, v in _walk(payload):
        lk = k.lower()
        if lk in CLASS_KEYS and isinstance(v, (list, str)):
            if isinstance(v, list):
                class_queries.extend(str(x) for x in v)
            else:
                class_queries.extend(re.split(r"[、，,;；/]|(?<!\d)\s+(?!\d)", str(v)))
        elif lk in DIRECTION_KEYS and isinstance(v, (list, str)):
            if isinstance(v, list):
                direction_queries.extend(str(x) for x in v)
            else:
                direction_queries.extend(re.split(r"[、，,;；/]", str(v)))
        elif lk in CATEGORY_KEYS and isinstance(v, (list, str)):
            # v1.2.7：类别参数（category/类别/大类）——十六测实测 planner 形态
            if isinstance(v, list):
                category_queries.extend(str(x) for x in v)
            else:
                category_queries.extend(re.split(r"[、，,;；/]", str(v)))
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            if lk in AGE_KEYS and age is None:
                age, age_source = _parse_age_value(v), "explicit"
            elif lk in GRADE_KEYS and grade_raw is None:
                grade_raw = str(v)
            elif lk in BUDGET_KEYS and budget is None:
                budget = _parse_budget_value(v)
            elif lk in TEXT_KEYS and isinstance(v, str):
                texts.append(v)

    blob = "\n".join(texts)

    if age is None:
        m = re.search(r"(\d+)\s*(?:周岁|岁)", blob)
        if m:
            age, age_source = int(m.group(1)), "explicit"
    if age is None:
        if grade_raw is not None:
            grade, gage = _parse_grade_value(grade_raw)
            if grade:
                age, age_source = gage, "grade"
        if age is None:
            for g in sorted(GRADE_AGE, key=len, reverse=True):
                if g in blob:
                    grade = g
                    age, age_source = GRADE_AGE[g], "grade"
                    break
    elif grade_raw is not None:
        grade, _gage = _parse_grade_value(grade_raw)
    if budget is None:
        budget = _budget_from_text(blob)

    if not class_queries and blob:
        for _pos, _key, target in _match_names_in_text(blob)[:12]:
            class_queries.append(target)

    # 方向归一：标准方向名直接采用；带前缀/后缀的自然写法（如"艺术感知方向"）
    # 剥壳后采用；班名/俗称（如"游泳"）归一到所属方向；类别词（如"艺术类"）转类别展开（v1.2.7）
    directions, unmatched_directions = [], []
    for d in direction_queries:
        d = str(d).strip()
        if not d:
            continue
        if d in VALID_DIRECTIONS:
            resolved = d
        else:
            nd = _norm_direction(d)
            if nd in VALID_DIRECTIONS:
                resolved = nd
            else:
                std = _resolve_query(nd) or _resolve_query(d)
                resolved = CLASS_BY_NAME[std][2] if std in CLASS_BY_NAME else None
        if resolved:
            if resolved not in directions:
                directions.append(resolved)
        else:
            cats = _resolve_category(d)
            if cats:
                category_queries.append(d)
            else:
                unmatched_directions.append(d)

    direction_packages = [_direction_package(d, age, budget) for d in directions]

    # 去重（保序）；类别词（如 classes 里传"艺术类"）转类别展开（v1.2.7）
    seen, queries = set(), []
    for q in class_queries:
        q = str(q).strip()
        if not q or q in seen:
            continue
        seen.add(q)
        if _resolve_query(q) is None and _resolve_category(q):
            category_queries.append(q)
        else:
            queries.append(q)

    # 类别展开（去重：同一组大类只展开一次）
    category_packages, seen_cats = [], []
    for q in category_queries:
        cats = _resolve_category(q)
        if not cats:
            continue
        key = tuple(cats)
        if key in seen_cats:
            continue
        seen_cats.append(key)
        category_packages.append(_category_package(q, cats, age, budget))

    if not queries and not direction_packages and not category_packages:
        out = {"ok": False, "reason": "classes_not_found",
               "hint": "未从上下文识别到具体兴趣班名。请向家长确认想了解的具体班级"
                       "（如游泳、钢琴、围棋），禁止猜测班名。",
               "age": age, "age_source": age_source or "none",
               "budget": budget, "budget_known": budget is not None,
               "classes": []}
        if unmatched_directions:
            out["unmatched_directions"] = unmatched_directions
            out["hint"] = (out["hint"] + "方向名未识别：" + "、".join(unmatched_directions)
                           + "（标准方向：" + "、".join(VALID_DIRECTIONS) + "）。")
        return out

    results, unmatched = [], []
    for q in queries:
        std = _resolve_query(q)
        if std is None:
            unmatched.append(q)
            results.append({"query": q, "name": None, "matched": False,
                            "hint": "未在速查表、风险资料与类型库收录清单中找到该班名，请回问家长确认具体项目"})
            continue
        if std in CLASS_BY_NAME:
            results.append(_class_result(q, std, age, budget))
        elif std in RISK_BY_NAME:
            results.append(_risk_extra_result(q, std, age))
        else:
            results.append(_offtable_result(q, std))

    out = {
        "ok": True,
        "age": age, "age_source": age_source or "none", "grade": grade,
        "budget": budget, "budget_known": budget is not None,
        "classes": results, "unmatched": unmatched,
        "output_reminders": OUTPUT_REMINDERS,
    }
    if direction_packages:
        out["direction_packages"] = direction_packages
    if category_packages:
        out["category_packages"] = category_packages
    if unmatched_directions:
        out["unmatched_directions"] = unmatched_directions
    if all(not r["matched"] for r in results) and not direction_packages and not category_packages:
        out.update({"ok": False, "reason": "classes_not_found",
                    "hint": "识别到的班名均不在收录范围内。请回问家长具体项目"
                            "（速查表共收录 75 个小学阶段兴趣班），禁止编造数据。"})
        return out
    if age is None:
        out["age_missing"] = True
        out["followup"] = ("孩子年龄未采集，无法执行年龄硬验证（age_ok=null）。"
                           "请按“最多追问一轮”规则补问孩子年龄或年级；若本轮补问后仍无，"
                           "按 references_08 第十节兜底输出。"
                           "若本次仅为陪练强度/费用查询（流程二），直接使用 classes[] 数据即可，"
                           "无需补问。")
    return out


def _selftest():
    cases = [
        # (name, payload, checks: list of (path, expected))
        ("年龄不足", {"classes": ["素描"], "age": 5},
         [("ok", True), ("classes/0/name", "素描 / 色彩（应试）"),
          ("classes/0/age_ok", False), ("classes/0/age_status", "below_floor")]),
        ("预算不足不排除", {"classes": ["马术"], "age": 8, "budget": 0.3},
         [("ok", True), ("classes/0/name", "马术"), ("classes/0/age_ok", True),
          ("classes/0/budget_sufficient", False),
          ("classes/0/budget_note", "contains:可能超出您0.3万元的预算")]),
        ("预算未提供", {"classes": ["游泳"], "age": 6},
         [("classes/0/budget_sufficient", None), ("classes/0/budget_note", "预算未提供，按年费区间列出")]),
        ("别名匹配", {"query": "孩子5岁，想学画画"},
         [("ok", True), ("age", 5), ("classes/0/name", "儿童创意美术"),
          ("classes/0/age_ok", True), ("classes/0/age_status", "ok")]),
        ("硬红线取严-吉他", {"classes": ["吉他"], "age": 6},
         [("classes/0/age_ok", False), ("classes/0/age_floor", 8),
          ("classes/0/speed_table_age", 7), ("classes/0/risk_note", "contains:琴箱")]),
        ("风险独有班-大提琴", {"classes": ["大提琴"], "age": 5},
         [("ok", True), ("classes/0/in_speed_table", False),
          ("classes/0/age_ok", False), ("classes/0/fee_range", None)]),
        ("表外班-中考体育集训", {"classes": ["中考体育集训"], "age": 9},
         [("ok", True), ("classes/0/matched", True), ("classes/0/in_speed_table", False),
          ("classes/0/age_ok", None), ("classes/0/age_status", "off_table"),
          ("classes/0/fee_range", None), ("classes/0/advantage", None),
          ("classes/0/category", "类型库收录（速查表外）")]),
        ("表外班-低龄段后缀归一", {"classes": ["亲子游泳（低龄段）", "奥尔夫音乐启蒙（低龄段）"], "age": 5},
         [("ok", True), ("classes/0/name", "亲子游泳"),
          ("classes/1/name", "奥尔夫音乐启蒙"),
          ("classes/0/in_speed_table", False), ("classes/1/in_speed_table", False)]),
        ("表外班+表内班混合", {"classes": ["游泳", "美术艺考"], "age": 7},
         [("ok", True), ("classes/0/in_speed_table", True),
          ("classes/0/age_ok", True), ("classes/1/in_speed_table", False),
          ("classes/1/fee_range", None)]),
        ("未知班", {"classes": ["某某班"], "age": 7},
         [("ok", False), ("reason", "classes_not_found"), ("classes/0/matched", False)]),
        ("年级换算", {"classes": ["游泳"], "grade": "二年级"},
         [("age", 7), ("age_source", "grade"), ("classes/0/age_ok", True)]),
        ("未到切入年龄", {"classes": ["游泳"], "age": 4},
         [("classes/0/age_ok", True), ("classes/0/age_status", "below_recommended")]),
        ("年龄缺失不判失败", {"classes": ["钢琴"]},
         [("ok", True), ("age_missing", True), ("followup", "contains:补问"),
          ("classes/0/matched", True), ("classes/0/age_ok", None)]),
        ("无班名", {"query": "孩子喜欢动，报什么班好"},
         [("ok", False), ("reason", "classes_not_found")]),
        ("文本预算+多班", {"query": "预算一年8000，看看围棋和马术，孩子8岁"},
         [("ok", True), ("budget", 0.8), ("age", 8),
          ("classes/0/name", "围棋"), ("classes/1/name", "马术"),
          ("classes/1/budget_sufficient", False),
          ("classes/1/budget_note", "contains:可能超出您0.8万元的预算")]),
        ("长词优先", {"query": "想学乐高机器人"},
         [("classes/0/name", "机器人（乐高/WeDo/EV3/Spike）")]),
        ("流程二陪练查询", {"classes": ["钢琴", "围棋", "游泳"]},
         [("classes/0/escort", "高"), ("classes/0/escort_rank", 4),
          ("classes/2/escort", "低")]),
        ("消息列表含assistant不污染", {"messages": [
            {"role": "assistant", "content": "推荐游泳和篮球"},
            {"role": "user", "content": "钢琴适合6岁孩子吗"}]},
         [("ok", True), ("age", 6), ("classes/0/name", "钢琴"), ("len:classes", 1)]),
        ("平台stdin结构化参数", {"query": {"sha256": "x", "length": 40, "preview": "…"},
                             "parameters": {"classes": ["演讲班"], "age": 8, "grade": "三年级",
                                            "budget": 10000},
                             "messages": [{"role": "user",
                                           "content": {"sha256": "y", "preview": "…"}}]},
         [("ok", True), ("age", 8), ("age_source", "explicit"), ("grade", "三年级"),
          ("budget", 1.0), ("classes/0/name", "演讲与口才"),
          ("classes/0/age_ok", True), ("classes/0/budget_sufficient", True)]),
        ("阿拉伯数字年级", {"query": "孩子今年3年级，想学游泳"},
         [("ok", True), ("age", 8), ("age_source", "grade"), ("grade", "3年级"),
          ("classes/0/name", "游泳")]),
        ("预算单位元转万", {"classes": ["游泳"], "budget": 10000},
         [("budget", 1.0), ("classes/0/budget_note", "contains:1万元的预算内")]),
        ("方向数据包", {"t11_directions": ["艺术感知", "体育竞技", "游泳"], "age": 8, "budget": 1},
         [("ok", True), ("len:direction_packages", 2),
          ("direction_packages/0/direction", "艺术感知"),
          ("direction_packages/1/direction", "体育竞技"),
          ("direction_packages/1/classes/0/name", "花样跳绳")]),
        ("方向名后缀归一-十一测2", {"directions": ["艺术感知方向", "主修语言沟通", "体育竞技赛道"], "age": 8},
         [("ok", True), ("len:direction_packages", 3),
          ("direction_packages/0/direction", "艺术感知"),
          ("direction_packages/1/direction", "语言沟通"),
          ("direction_packages/2/direction", "体育竞技")]),
        ("已过切入区间-past_recommended-十四测", {"classes": ["儿童创意美术"], "age": 8},
         [("ok", True), ("classes/0/name", "儿童创意美术"),
          ("classes/0/age_ok", True), ("classes/0/age_status", "past_recommended"),
          ("classes/0/age_note", "contains:已过建议切入区间")]),
        ("排序-过窗班后排未到下限班-十四测", {"directions": ["艺术感知"], "age": 8},
         [("ok", True), ("len:direction_packages", 1),
          ("direction_packages/0/classes/0/name", "合唱团"),
          ("direction_packages/0/classes/18/name", "管乐（长笛/单簧管/萨克斯）"),
          ("direction_packages/0/classes/18/age_status", "below_recommended"),
          ("direction_packages/0/classes/19/name", "儿童创意美术"),
          ("direction_packages/0/classes/19/age_status", "past_recommended")]),
        ("四级阶梯-预算充足窗口内置顶-六十三测", {"directions": ["体育竞技"], "age": 9, "budget": 2.0},
         [("ok", True), ("len:direction_packages", 1),
          ("direction_packages/0/classes/0/name", "滑板"),
          ("direction_packages/0/classes/0/age_status", "ok"),
          ("direction_packages/0/classes/0/budget_sufficient", True),
          ("direction_packages/0/classes/5/name", "花样跳绳"),
          ("direction_packages/0/classes/5/age_status", "past_recommended")]),
        ("四级阶梯-过窗预算充足先于窗口内预算不足",
         {"directions": ["体育竞技"], "age": 9, "budget": 0.4},
         [("ok", True),
          ("direction_packages/0/classes/0/name", "花样跳绳"),
          ("direction_packages/0/classes/0/age_status", "past_recommended"),
          ("direction_packages/0/classes/0/budget_sufficient", True),
          ("direction_packages/0/classes/6/name", "滑板"),
          ("direction_packages/0/classes/6/age_status", "ok"),
          ("direction_packages/0/classes/6/budget_sufficient", False)]),
        ("类别参数-艺术类-十六测", {"parameters": {"age": 8, "city": "杭州",
                                              "grade": "三年级", "budget": 10000,
                                              "category": "艺术类"},
                                  "query": {"length": 20, "sha256": "x", "preview": "…"}},
         [("ok", True), ("len:category_packages", 1),
          ("category_packages/0/expanded_categories",
           ["音乐表演类", "舞蹈体态类", "美术创作类"]),
          ("category_packages/0/class_count", 28)]),
        ("classes 传类别词+过窗排序-十六测", {"classes": ["艺术类"], "age": 8},
         [("ok", True), ("len:category_packages", 1),
          ("category_packages/0/class_count", 28),
          ("category_packages/0/classes/0/age_status", "ok"),
          ("category_packages/0/classes/19/name", "儿童创意美术"),
          ("category_packages/0/classes/19/age_status", "past_recommended"),
          ("category_packages/0/classes/19/age_note", "contains:已过建议切入区间")]),
        ("表演类类别别名-二十测（判型新词：音乐表演+舞蹈体态+语言表达 26 班，播音主持/戏剧表演属语言表达类）",
         {"category": "表演类", "age": 10, "city": "杭州"},
         [("ok", True), ("len:category_packages", 1),
          ("category_packages/0/expanded_categories",
           ["音乐表演类", "舞蹈体态类", "语言表达类"]),
          ("category_packages/0/class_count", 26),
          ("category_packages/0/classes/0/age_status", "ok")]),
    ]

    def get(d, path):
        if path.startswith("len:"):
            return len(d[path[4:]])
        cur = d
        for p in path.split("/"):
            cur = cur[int(p)] if isinstance(cur, list) else cur[p]
        return cur

    passed = failed = 0
    for name, payload, checks in cases:
        try:
            res = main(payload)
            ok = True
            for path, exp in checks:
                got = get(res, path)
                if isinstance(exp, str) and exp.startswith("contains:"):
                    good = exp[9:] in str(got)
                else:
                    good = got == exp
                if not good:
                    ok = False
                    print(f"FAIL [{name}] {path}: got={got!r} want={exp!r}")
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL [{name}] exception: {exc!r}")
    print(f"lookup selftest: {passed} passed, {failed} failed, total {len(cases)}")
    return failed == 0


def _run_cli(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--classes")
    parser.add_argument("--age")
    parser.add_argument("--grade")
    parser.add_argument("--budget")
    parser.add_argument("--directions")
    parser.add_argument("--category")
    parser.add_argument("--query")
    parser.add_argument("--selftest", action="store_true")
    known, _unknown = parser.parse_known_args(argv)

    if known.selftest:
        _selftest()
        return None

    # 始终读取 stdin（平台契约：stdin JSON 上下文），CLI 参数随后合并并覆盖同名字段
    payload = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    payload.update(loaded)
            except Exception:  # noqa: BLE001
                payload["query"] = raw
    if known.query:
        payload["query"] = known.query
    if known.classes:
        payload["classes"] = known.classes
    if known.age:
        payload["age"] = known.age
    if known.grade:
        payload["grade"] = known.grade
    if known.budget:
        payload["budget"] = known.budget
    if known.directions:
        payload["directions"] = known.directions
    if known.category:
        payload["category"] = known.category
    return main(payload)


if __name__ == "__main__":
    try:
        _res = _run_cli(sys.argv[1:])
        if _res is not None:
            print(json.dumps(_res, ensure_ascii=False))
    except Exception as _exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "reason": "script_error",
                          "hint": f"脚本执行异常（{_exc}），请按 references_04 速查表人工核对班级信息。"},
                         ensure_ascii=False))
