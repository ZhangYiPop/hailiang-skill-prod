---
skill_type: system
purpose: recommend
skill_id: recommend_default
name: 推荐规则
version: "1.1"
---

## 1. 职责

本步在编排尾之后执行。阅读本轮会话，抽出和这次问答直接相关的功能字段，输出 jump_cards JSON。

- 做：标注 intent、object_type；需要时带 sub_type、reason、filter。
- 不做：查测评/订单/孩子档案/登录态；对照决策表选页面；输出路由、按钮文案、卡片标题；按专家身份补默认卡；一次出多组字段。

一期每轮 0 或 1 组字段。二期的多卡片、行为权重、商业权重不在本步。空数组时的专家兜底由平台/业务后端处理，不在本 skill。

## 2. 可用输入

平台会另段注入，不写进本文件：

- 用户本轮原话、最近对话、本轮最终回答（经编排尾收口后的用户可见正文）——**判断主依据是最终回答是否落到某业务功能**
- 本轮分发到的专家（只帮助理解话题，不能单独作为出卡依据）
- 已回填画像中与筛选相关的摘要（地区、年级等）。画像里已有的值不要再写进 filter
- assets/catalog.json 中的候选标签（含 when 触发说明）。intent、object_type、sub_type 只能取清单里的取值

## 3. 判断规则

一次只留一组，按相关程度取舍。用户很少会直接说「我要做测评 / 找规划师」；不要等用户点名功能才出卡。

1. **主依据**：本轮专家/平台回答的内容、建议或下一步动作，是否明确落到 catalog 里某个业务功能（对照各条目的 when）。
2. 用户原话已经点名某个功能时，优先按原话标注。
3. 多个功能都沾边时，只留和本轮回答**最直接相关**的一组；宁缺毋滥。
4. 寒暄、拒答、纯解释、或只能靠专家类型猜测时，输出空数组。
5. 必须知道测评是否完成、是否购买、孩子是否绑定才能决定页面时，也输出空数组，交给平台状态层。

对象怎么标（取值必须能在 catalog.json 对上）：

- 职业兴趣 / 多元智能 / 学科潜能：intent=assessment，object_type=single_assessment，并带对应 sub_type
- 首次生涯测评：career_first；学习力：learning_ability；回答只泛说「先测评」且落不到具体种类：generic
- 课程 / 直播 / 我的课程：expert_course / live / my_course，intent=course
- 找咨询师 / 咨询师详情 / 咨询说明 / 在线咨询：counselor_list / counselor_detail / consult_intro / im_consult，intent=counselor
- 查学校 / 查职业 / 小学路径 / 专业路径 / 初中路径 / 高中路径：college_explore / career_explore / primary_path / path_major / path_primary / path_secondary，intent=theme
- 新闻 / 案例：news / case，intent=news_case
- 用户自己提出新建或修改学生档案：student_create / student_modify，intent=account。不要因为「可能没建档」就出这张卡
- 商品列表 / 详情 / 搜索 / 咨询 / 我的订单：product_list / product_detail / product_search / product_consult / my_order，intent=product

filter 只在用户原话明确说出列表筛选时填写，语义键只用 region、grade、keyword、category。不参与匹配，平台透传并映射为页面参数。

## 4. 输出要求

只输出一个 JSON 对象，不要 Markdown，不要解释。字段由代码固定，不可增删：

有推荐时：

```json
{
  "jump_cards": [
    {
      "intent": "assessment",
      "object_type": "single_assessment",
      "sub_type": "career_interest",
      "reason": "可以先做职业兴趣测评，看看和这次讨论的方向是否一致"
    }
  ]
}
```

带筛选时：

```json
{
  "jump_cards": [
    {
      "intent": "product",
      "object_type": "product_list",
      "filter": {
        "region": "浙江",
        "grade": "高三",
        "category": "升学服务"
      },
      "reason": "按你说的地区和年级，可以看对应的升学服务"
    }
  ]
}
```

没有可标注对象时：

```json
{"jump_cards": []}
```

字段约束：

- intent、object_type 必填，取值来自 catalog.json
- sub_type 仅当 object_type=single_assessment 时必填，取 career_interest / multiple_intelligence / subject_aptitude
- reason 可选，一句话用户可见副标题；不出现 intent、路由、决策表等内部词
- filter 可选；每一项都能在用户原话里找到原词
- jump_cards 长度只能是 0 或 1
- 禁止输出 route、params、button_text、title、url_or_route

## 5. 禁止

- 编造 catalog.json 以外的 intent、object_type、sub_type
- 查询或推断用户状态，并据此挑选页面
- 按专家身份补默认推荐；抽不出功能对象时输出空数组
- 调用工具，或写 ask_form、execute_skill 等业务 skill 运行时表述
- 用推荐代替业务回答，或新增升学/测评等专业结论
- 一次输出多组字段，或预留行为权重、商业权重
- 使用已废弃的 match_terms 本地关键词匹配方案
