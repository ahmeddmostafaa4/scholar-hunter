"""Read your GUC transcript from the student portal.

    export GUC_USERNAME="your.username"
    export GUC_PASSWORD="your.password"
    python example.py

The portal is slow and rate-limited. It needs roughly a minute between requests,
so this example reads a single year. Never paste your password into this file.
"""

from guc_portal import GucPortal


def main() -> None:
    portal = GucPortal()  # reads GUC_USERNAME / GUC_PASSWORD from the environment

    print("Study years on offer:")
    for value, label in portal.available_years():
        print(f"  {value:>3}  {label}")

    # One year, the fast path. Pick a value from the list above.
    transcript = portal.get_transcript_year("22")  # 2024-2025
    print("\nCumulative GPA:", transcript.cumulative_gpa)
    for semester, rows in transcript.by_semester().items():
        print(f"\n{semester}")
        for row in rows:
            print(f"  {row.grade:>3}  {row.numeric:>4}  {row.hours}h  {row.course}")

    # The whole transcript (slow: it waits ~60s between years):
    # full = portal.get_transcript()
    # print("all courses:", len(full.rows))

    # --- previous grades: the detailed coursework marks for a past course ---
    # Leave a gap between portal calls (it is rate-limited).
    seasons = portal.available_seasons()  # [("64", "Winter 2024"), ...]
    print("\nseasons:", seasons[:4])
    # courses = portal.list_previous_courses("64")   # what was taught that season
    # marks = portal.get_previous_grades("64", "168")
    # print(marks.course, "->", marks.percentages)
    # for item in marks.items:
    #     print("  ", item.assessment, item.grade, item.evaluator)

    # --- current grades: same idea for this term (empty between terms) ---
    # for value, label in portal.list_current_courses():
    #     print(label, portal.get_current_grades(value).items)


if __name__ == "__main__":
    main()
