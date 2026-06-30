"""
5AI 브리핑 패널 — 스트레스 테스트 스크립트
=================================================
실시간 API 없이 더미 데이터를 강제 주입해 generate_ai_briefing()의
논리 연산과 출력 문구를 검증한다.

실행:  python test_ai_briefing.py

특징:
- 실제 quant_dashboard.py 안의 generate_ai_briefing 함수 '원본'을 추출해 테스트한다
  (streamlit 미설치 환경에서도 동작 — 함수 자체는 st.* 의존 없음).
- 테스트 케이스 A(킬 스위치), B(게이트 개방), 그리고 Null 결측 케이스까지 실행.
"""
import ast
import textwrap

SRC_FILE = "quant_dashboard.py"
TARGET_FN = "generate_ai_briefing"


def _load_function(src_path: str, fn_name: str):
    """대시보드 소스에서 지정 함수의 소스만 추출해 독립 실행 가능한 객체로 반환."""
    with open(src_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=src_path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            fn_src = ast.get_source_segment(open(src_path, encoding="utf-8").read(), node)
            ns: dict = {}
            exec(textwrap.dedent(fn_src), ns)
            return ns[fn_name]
    raise RuntimeError(f"{fn_name} 함수를 {src_path}에서 찾지 못했습니다.")


def _print_result(title: str, res: dict):
    light_icon = {"green": "🟢", "amber": "🟡", "red": "🔴"}.get(res["light"], "⚪")
    print(f"\n{'='*64}\n[{title}]  판정등: {light_icon} {res['light'].upper()}")
    print(f"종합 판정: {res['verdict']}")
    for line in res["lines"]:
        print(f"   {line}")
    print("=" * 64)


def run():
    gen = _load_function(SRC_FILE, TARGET_FN)

    # ── 테스트 케이스 A : 킬 스위치 발동 장세 ──────────────────────────
    # 환율 1,560 / 외인 -1조 순매도 / 1위 종목 43점
    case_a = gen(
        krw=1560,
        foreign_net_krw=-1_000_000_000_000,
        top1={"종합점수": 43, "정배열": "❌"},
    )
    _print_result("CASE A — 킬 스위치 발동 장세", case_a)
    assert case_a["light"] == "red", "❌ A는 red(보류)여야 함"
    assert "보류" in case_a["verdict"]

    # ── 테스트 케이스 B : 매크로 게이트 개방 장세 ─────────────────────
    # 환율 1,510 / 외인 +5,000억 순매수 / 1위 종목 85점 정배열
    case_b = gen(
        krw=1510,
        foreign_net_krw=500_000_000_000,
        top1={"종합점수": 85, "정배열": "✅"},
    )
    _print_result("CASE B — 매크로 게이트 개방 장세", case_b)
    assert case_b["light"] == "green", "❌ B는 green(승인)이어야 함"
    assert "승인" in case_b["verdict"]

    # ── 테스트 케이스 C : 데이터 결측(Null) 방어 ──────────────────────
    # 모든 입력 None → 예외 없이 '데이터 확인 필요' 출력 + amber
    case_c = gen(krw=None, foreign_net_krw=None, top1=None)
    _print_result("CASE C — 데이터 전면 Null(결측) 방어", case_c)
    assert case_c["light"] == "amber", "❌ C는 amber(신중)여야 함"

    # ── 테스트 케이스 D : 부분 결측(환율만 정상) ──────────────────────
    case_d = gen(krw=1490, foreign_net_krw=None, top1={"종합점수": 72, "정배열": "✅"})
    _print_result("CASE D — 외인 수급만 결측(부분 출력)", case_d)
    assert case_d["light"] == "amber"

    print("\n✅ 전체 4개 케이스 통과 — 논리 연산·문구·결측 방어 정상")


if __name__ == "__main__":
    run()
