"""PR 이해 리포트 정적 페이지 빌더.

전체 파이프라인 기준 CI·리포트 구간(cross/ci_release)의 보조 도구로,
template.html의 /*__REPORT_DATA__*/ 플레이스홀더에 report.json을 주입해
완성된 HTML을 stdout으로 출력합니다. 스키마 검증은 워크플로우의
check-jsonschema 스텝이 담당하며 이 스크립트는 주입만 수행합니다.

사용법: python inject.py <template.html> <report.json> > index.html
"""

import json
import sys
from pathlib import Path

PLACEHOLDER = "/*__REPORT_DATA__*/"


def main() -> int:
    template_path, report_path = Path(sys.argv[1]), Path(sys.argv[2])
    data = json.dumps(
        json.loads(report_path.read_text(encoding="utf-8")), ensure_ascii=False
    )
    # </script> 조기 종료 방지
    data = data.replace("</", "<\\/")
    html = template_path.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        print(f"placeholder {PLACEHOLDER} not found in {template_path}", file=sys.stderr)
        return 1
    sys.stdout.write(html.replace(PLACEHOLDER, data, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
