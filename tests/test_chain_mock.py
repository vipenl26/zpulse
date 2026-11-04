"""Tests for zpulse.chain (MockChain, concurrency) and the zwallet.py parsers (no network)."""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zpulse import chain  # noqa: E402

zwallet = chain._zwallet_module()

TXA = "a" * 64
TXB = "b" * 64
TXC = "c" * 64
UA1 = "utest1qqqfirstaddress"
HB = "ZP1 HB 0123456789abcdef 3 1800000000 " + "Q" * 86 + "=="

# Shape follows zcash-devtool src/commands/wallet/list_tx.rs (text mode) and
# zcash_protocol's `impl Debug for Memo` (`Memo::Text("...")`, unescaped).
LIST_TX = f"""Transactions:
{TXA}
     Mined: 4581500 (2026-11-04 7:12:23.0 +00:00:00)
    Amount:   0.00030000 TAZ
  Fee paid: Unknown
  Sent 0 notes, received 2 notes, 2 memos
  Output 0 (Orchard)
    Value:   0.00010000 TAZ
    Received by account: cc10a6a1-a7d2-444f-a77e-24e32480c067 (w1)
    To: {UA1}
    Memo: Memo::Text("{HB}")
  Output 1 (Orchard)
    Value:   0.00020000 TAZ
    Received by account: cc10a6a1-a7d2-444f-a77e-24e32480c067 (w1)
    Memo: Memo::Text("tricky "quotes") and \\\\ backslash")
{TXB}
     Mined: 4581510 (2026-11-04 7:20:00.0 +00:00:00)
    Amount:  -0.00011000 TAZ
  Fee paid:   0.00010000 TAZ
  Sent 1 notes, received 1 notes, 1 memos
  Output 0 (Orchard)
    Value:   0.00001000 TAZ
    Sent from account: cc10a6a1-a7d2-444f-a77e-24e32480c067 (w1)
    To: utest1release
    Memo: Memo::Text("ZP1 SH ...")
  Output 1 (Orchard)
    Value:   1.23456789 TAZ (Change)
    Memo: Memo::Empty
{TXC}
   Unmined (expiry height: 4581600)
    Amount:   0.00010000 TAZ
  Fee paid: Unknown
  Sent 0 notes, received 1 notes, 0 memos
  Output 0 (Sapling)
    Value:   0.00010000 TAZ
    Received by account: cc10a6a1-a7d2-444f-a77e-24e32480c067 (w1)
"""


def _ledger() -> str:
    return os.path.join(tempfile.mkdtemp(), "mockchain.json")


def test_mock_send_receive_balance():
    led = _ledger()
    p, w1, w2 = (chain.open_chain("mock", n, led) for n in ("principal-x", "watcher-w1", "watcher-w2"))
    a1, a2 = w1.new_address("vaultA"), w2.new_address("vaultA")
    assert a1.startswith("mock:watcher-w1:vaultA:") and a1 != w1.new_address("vaultA")
    assert p.height() == 0 and w1.received() == []
    txid = p.send([(a1, 10_000, "hello"), (a2, 10_000, "hello")])
    assert p.height() == 1
    r = w1.received()
    assert len(r) == 1 and r[0]["txid"] == txid and r[0]["memo"] == "hello" and r[0]["to"] == a1
    assert r[0]["height"] == 1 and r[0]["amount_zat"] == 10_000 and r[0]["time"] > 0
    assert w2.received()[0]["txid"] == txid  # same tx, one output each
    assert p.received() == []
    w1.send([(p.new_address("change"), 1_000, None)])
    assert w1.received(min_height=2) == [] and len(p.received()) == 1
    assert w1.balance_zat() == chain.MOCK_START_ZAT + 9_000
    assert p.balance_zat() == chain.MOCK_START_ZAT - 19_000
    # a node named "watcher-w" must not see "watcher-w1" outputs
    assert chain.MockChain("watcher-w", led).received() == []


def test_mock_rejects_bad_sends():
    c = chain.MockChain("n", _ledger())
    for outs in ([], [("mock:x:y:z", 0, None)], [("mock:x:y:z", 1, "x" * 513)],
                 [("mock:x:y:z", chain.MOCK_START_ZAT + 1, None)]):
        try:
            c.send(outs)
            raise AssertionError(outs)
        except (ValueError, AssertionError, RuntimeError) as e:
            if isinstance(e, AssertionError) and e.args == (outs,):
                raise


def test_mock_env_override():
    led = _ledger()
    old = os.environ.get("ZPULSE_MOCK_LEDGER")
    os.environ["ZPULSE_MOCK_LEDGER"] = led
    try:
        assert str(chain.MockChain("n").path) == led
    finally:
        if old is None:
            del os.environ["ZPULSE_MOCK_LEDGER"]
        else:
            os.environ["ZPULSE_MOCK_LEDGER"] = old


def test_mock_concurrent_processes():
    led = _ledger()
    procs, per = 8, 15
    code = (f"import sys; sys.path.insert(0, {ROOT!r}); from zpulse.chain import MockChain\n"
            f"n = sys.argv[1]; c = MockChain(n, {led!r})\n"
            f"for i in range({per}):\n"
            f"    c.send([('mock:sink:x:000000', 1, n + str(i))]); c.received(); c.height()\n")
    ps = [subprocess.Popen([sys.executable, "-c", code, f"node{i}"]) for i in range(procs)]
    assert all(p.wait(timeout=60) == 0 for p in ps)
    sink = chain.MockChain("sink", led)
    rx = sink.received()
    assert sink.height() == procs * per == len(rx)
    assert [t["height"] for t in rx] == list(range(1, procs * per + 1))
    assert len({t["memo"] for t in rx}) == procs * per


def test_zip321_uri():
    uri = zwallet.zip321_uri([("utest1aaa", 10_000, "ZP1 HB x"), ("utest1bbb", 150_000_000, None),
                              ("utest1ccc", 100_000_000, "é?&=")])
    assert uri.startswith("zcash:utest1aaa?amount=0.0001&memo=")
    assert "&address.1=utest1bbb&amount.1=1.5&" in uri
    assert "&address.2=utest1ccc&amount.2=1&memo.2=" in uri
    memo2 = uri.split("memo.2=")[1]
    assert "=" not in memo2 and "+" not in memo2 and "/" not in memo2
    assert base64.urlsafe_b64decode(memo2 + "=" * (-len(memo2) % 4)).decode() == "é?&="
    assert zwallet._zec(1) == "0.00000001" and zwallet._zec(123_456_789) == "1.23456789"


def test_parse_memo_line():
    assert zwallet.parse_memo_line(f'Memo: Memo::Text("{HB}")') == HB
    assert zwallet.parse_memo_line('Memo: Text(TextMemo("a \\"q\\" \\\\ b"))') == 'a "q" \\ b'
    assert zwallet.parse_memo_line('Memo: Text("plain")') == "plain"
    assert zwallet.parse_memo_line("Memo: Memo::Empty") is None
    assert zwallet.parse_memo_line("Memo: Memo::Arbitrary(00 01)") is None


def test_parse_received_fixture():
    rows = zwallet.parse_received(LIST_TX)
    assert [(r["txid"], r["amount_zat"]) for r in rows] == [(TXA, 10_000), (TXA, 20_000)]
    assert rows[0] == {"txid": TXA, "height": 4581500, "time": 1793776343, "to": UA1,
                       "amount_zat": 10_000, "memo": HB}
    assert rows[1]["to"] == "" and rows[1]["memo"] == 'tricky "quotes") and \\\\ backslash'


def test_devtool_chain_with_fake_binary():
    calls = []

    def fake_run(args, timeout=0):
        calls.append(args)
        cmd = args[3]
        if cmd == "enhance":  # received() fetches full tx data (memos) before list-tx
            return ""
        if cmd == "list-tx":
            return LIST_TX
        if cmd == "generate-address":
            return "Account AccountUuid(..)\n     Address: utest1fresh\n Address type: unified\n"
        if cmd == "list-accounts":
            return "Account x (birthday height 1)\n     UIVK: uivk1\n     UFVK: uviewtest1abc\n"
        if cmd in ("pay", "send"):
            return "Creating transaction...\nSending transaction...\n" + "d" * 64 + "\n"
        raise AssertionError(args)

    real, zwallet._run = zwallet._run, fake_run
    try:
        c = chain.open_chain("testnet", "watcher-w1")
        assert c.new_address("v") == "utest1fresh"
        assert c.wallet.ufvk() == "uviewtest1abc"
        assert len(c.received()) == 2 and c.received(min_height=4581501) == []
        assert c.send([("utest1a", 1000, "m1"), ("utest1b", 2000, None)]) == "d" * 64
        assert "--disable-confirmation" in calls[-1]
        uri = calls[-1][calls[-1].index("--payment-uri") + 1]
        assert uri.startswith("zcash:utest1a?amount=0.00001&memo=bTE&address.1=utest1b")
        assert c.send([("utest1a", 1000, "solo")]) == "d" * 64
        assert calls[-1][3] == "send" and calls[-1][calls[-1].index("--memo") + 1] == "solo"
    finally:
        zwallet._run = real


def _run_all() -> int:
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
