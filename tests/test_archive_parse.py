"""消息深度解析单测：disagree / redpacket / meeting / calendar / location / chatrecord"""
import sys
sys.path.insert(0, ".")

from app.collectors.archive import ArchiveCollector


def norm(plain):
    return ArchiveCollector._normalize(plain.get("seq", 1), plain)


def test_disagree():
    m = norm({"msgtype": "disagree", "msgid": "d1",
              "disagree": {"userid": "zhangsan", "reason": "不想被录"}})
    assert m.msg_type == "disagree"
    assert "zhangsan" in m.content_text
    assert "不想被录" in m.content_text
    assert m.raw is not None


def test_redpacket():
    m = norm({"msgtype": "redpacket", "msgid": "r1",
              "redpacket": {"type": 2, "wish": "新年快乐"}})
    assert "拼手气" in m.content_text
    assert "新年快乐" in m.content_text


def test_meeting():
    m = norm({"msgtype": "meeting", "msgid": "m1",
              "meeting": {"title": "季度会", "location": "会议室A"}})
    assert "季度会" in m.content_text
    assert "会议室A" in m.content_text


def test_calendar():
    m = norm({"msgtype": "calendar", "msgid": "c1",
              "calendar": {"title": "拜访客户", "start_time": 1700000000}})
    assert "拜访客户" in m.content_text
    assert "1700000000" in m.content_text


def test_location():
    m = norm({"msgtype": "location", "msgid": "l1",
              "location": {"title": "公司", "address": "北京",
                           "latitude": 39.9, "longitude": 116.4}})
    assert "公司" in m.content_text
    assert "39.9" in m.content_text and "116.4" in m.content_text


def test_chatrecord_sender():
    m = norm({"msgtype": "chatrecord", "msgid": "cr1",
              "chatrecord": {"title": "聊天记录", "item": [
                  {"msgtype": "text", "from": "alice", "text": {"content": "你好"}},
                  {"msgtype": "image", "from": "bob"},
              ]}})
    assert "alice" in m.content_text and "你好" in m.content_text
    assert "bob" in m.content_text and "[image]" in m.content_text
    assert m.raw is not None


if __name__ == "__main__":
    test_disagree()
    test_redpacket()
    test_meeting()
    test_calendar()
    test_location()
    test_chatrecord_sender()
    print("ALL ARCHIVE PARSE TESTS PASSED")
