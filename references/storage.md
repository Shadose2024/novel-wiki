# SQLite存取与续做

在作品工作目录保存数据库、来源快照、阅读包和交接；不往skill目录写作品数据。随附脚本仅用Python标准库。下述wiki.py指向本skill的scripts/wiki.py；实际调用使用其完整路径。只查接口时运行--help，无须读全部实现。

```text
python wiki.py --db 作品.sqlite create
python wiki.py --db 作品.sqlite status --limit 5
python wiki.py --db 作品.sqlite packs --out 阅读包目录
python wiki.py --db 作品.sqlite ingest --pack 阅读包.json --result 提取结果.json
python wiki.py --db 作品.sqlite search 对象名称
python wiki.py --db 作品.sqlite show record 记录ID
python wiki.py --db 作品.sqlite apply 修改.json
python wiki.py --db 作品.sqlite history record 记录ID
python wiki.py --db 作品.sqlite export --out 审阅目录
python wiki.py --db 作品.sqlite check
python wiki.py --db 作品.sqlite backup --out 新备份.sqlite
```

## 接入原文

create只建立空库，文件读取由agent处理。首次接入时按[schema.sql](../scripts/schema.sql)与[v2扩展](../scripts/confirmation.sql)，在开启外键的事务中写入source_document、source_scope及chapter。三个表分别保存来源版本、完整目录范围和章节快照。具体读取方法按文件选择。

- 来源用原始文件SHA-256区分版本；decoded_sha256对应保留的统一文本快照，locator_policy说明定位规则及映射文件的位置。
- 章节序号从1开始，按阅读顺序连续编号，与作品印刷章号分开。source_scope.total_chapters填完整来源目录数量；未知时为NULL，不以局部入库数量代替。
- 原文或转换后的固定文本快照必须保留。章节start_char/end_char采用该快照中的Unicode字符半开区间，长度须与raw_text一致。转换来源保存回查映射，例如EPUB内部文件与段落；不把转换后的位置冒充原文件位置。
- chapter新内容通常为unread/pending。来源字段不可覆盖；换版本重新建立来源及章节映射。分包按非空行编号，首段与标题一致时跳过标题，否则从p000处理正文。空白行若含空格也会占一个编号，以生成包为准。

来源与章节的接入是执行agent按文件完成的一次性工作；不要求提取模型编写SQL或计算偏移。

## 资料与正文修改

entity保存稳定对象；record保存fact/event/relation/alias/question；evidence关联原文；article保存正文。修改使用apply，避免丢失版本与历史。先show读取当前版本，新对象expected_version为null。

```json
{
  "reason": "说明本次修改依据",
  "editor": "执行者名称",
  "operations": [{
    "table": "record",
    "id": "记录ID",
    "expected_version": 1,
    "set": {"content": "修改后的内容", "review_status": "agent_checked"}
  }]
}
```

支持entity、record、article。record必备字段为kind、title、content、source_status、modality；对象字段subject_id/object_id可空。source_status用explicit/attributed/interpretation/unresolved，modality用actual/planned/general/hypothetical/unknown。分包导入会生成这些字段，不让提取模型填写数据库格式。

新增record另带evidence数组，每项填chapter_id、paragraph_start、paragraph_end、kind（narration/dialogue/thought/mixed），可填speaker_id与note；脚本据段落取原文。提供evidence时会替换全部证据，追加前先读取旧证据；participants同理。多证据归并必须保留所有仍有效出处。

entity的set使用name、kind、scope_note；article使用entity_id、title、body、scope_note，正文用[[记录ID]]绑定资料版本。修改记录后相关正文会被标记过期；重核正文后再次提交article。确认标签通过专门confirmation字段操作，见[确认规则](confirmation.md)。

别名使用kind=alias，subject_id指向对象、value_text为别名并附证据。合并重复资料先选择保留记录、汇集有效证据并调整正文引用；重复项用rejected和说明保留历史。按对象归并只改有依据的关联，不因同名全库替换。

## 进度与跨章关联

status只读统计已读范围、分包提交缺口、待复核记录、待归并主体、疑点、初步确定和过期正文。队列默认最多20项，使用limit控制输出。重复导入相同结果保留已有编辑；不同结果须通过apply修订。空items也计入该任务已提交。

分包提交不自动修改语义或阅读状态。agent实际完成后，在事务中维护chapter.reading_status、extraction_status、coverage_note；仅完成事件或细节一遍不能标为reviewed。excluded必须填写理由，缺文不能被当作已读正文。reading_task.status可在该包对应记录核对完成后更新为reviewed。

跨章联系写入record_link，关系为before/motivates/responds_to/clarifies/contradicts/corrects/part_of。corrects从新说法指向被纠正说法，before从早事件指向晚事件。涉及剧情时间与身份的解释先由agent核对证据。

上述进度及record_link目前通过agent的参数化SQL事务维护，apply尚不覆盖这些表；修改时同时向revision记录target_table、target_id、before_json、after_json、reason、editor，复合键用JSON表示。事务失败全部回滚。不要直接修改record来绕过apply的版本、证据和确认检查。

export生成审阅Markdown、review-data.json与progress.json。导出文件不代替数据库备份；review-data不含全文和完整引文。续做优先读status及交接，队列为空也不自动宣称语义无遗漏。已有人工整理而缺少reading_task时，先核对记录再补记进度，避免盲目重复提取。
