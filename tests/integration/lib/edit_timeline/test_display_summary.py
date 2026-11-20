"""YouReel: revision summaries read in English, the upstream Chinese ones kept on disk included.
YouReel：修订摘要以英文显示，包括磁盘上保留的上游中文摘要。"""

from lib.edit_timeline.service import MECHANICAL_CREATION_SUMMARY, _display_summary


def test_legacy_mechanical_summary_reads_in_english() -> None:
    assert _display_summary("按当前脚本机械新建：每个视频单元整段使用、全部硬切") == MECHANICAL_CREATION_SUMMARY


def test_legacy_copy_and_rollback_summaries_read_in_english() -> None:
    assert _display_summary("复制自「Full cut」的修订 3") == "Copied from 'Full cut' revision 3"
    assert _display_summary("回滚到修订 2，撤销之后的改动；回滚前的修订仍保留在历史里").startswith("Rolled back to revision 2,")


def test_other_summaries_unchanged() -> None:
    assert _display_summary("Moved c12 later") == "Moved c12 later"
