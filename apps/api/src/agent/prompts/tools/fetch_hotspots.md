获取当前热点、热榜、热搜或趋势话题。

当用户询问实时热点、近期热门话题，或要求基于热点进行选题、内容策划、趋势分析时调用。不要凭空编造实时热点；需要最新信息时应优先调用此工具。

参数说明：
- source：热点来源分组，可填 all、rss、tikhub、aihot，多个值用英文逗号分隔。
- rss_sources：媒体/RSS 来源，可填 all、36kr、cls、eeo、yicai、huxiu、jiemian、tmtpost、latepost、qbitai、leiphone、caixin、vista、ft、wsj、techcrunch、theverge、ifanr、stcn，多个值用英文逗号分隔。
- platforms：TikHub 平台，可填 all、douyin、bilibili、xiaohongshu、weibo，多个值用英文逗号分隔。

工具会自动按当前账号配置的允许热点来源取交集。采集后的候选热点会交给隔离的选题过滤子模型；子模型只接收账号配置中的“选题评分提示词”，以及每条热点的标题、原文链接、摘要、源平台和发布时间，并直接返回筛选结果，不接收账号定位、内容提示词或会话历史。收到 result 后，只结合当前用户消息补充必要的会话承接语并整理展示格式，然后输出该结果；不要重新筛选、评分、排序、改写理由或补充候选热点，也不要声称结果基于账号定位。
