import contextlib
import csv
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import apply_log as al  # noqa: E402

TODAY = date(2026, 10, 13)


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = os.path.join(self.dir.name, "data", "applications.json")
        self.today = TODAY

    def run_cli(self, *argv, today=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = al.main(["--file", self.file] + list(argv), today=today or self.today)
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def data(self):
        return al.load(self.file)

    def add(self, company="Acme", role="ML Engineer", *extra, today=None):
        code, out, err = self.run_cli("add", company, role, *extra, today=today)
        self.assertEqual(code, 0, err)
        return out


class DateTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(al.parse_date("today", TODAY), TODAY)
        self.assertEqual(al.parse_date("Tomorrow", TODAY), date(2026, 10, 14))
        self.assertEqual(al.parse_date("+3d", TODAY), date(2026, 10, 16))
        self.assertEqual(al.parse_date("+2w", TODAY), date(2026, 10, 27))
        self.assertEqual(al.parse_date("2026-12-31", TODAY), date(2026, 12, 31))
        self.assertEqual(al.parse_date("2026-01-05", TODAY), date(2026, 1, 5))   # the past is allowed

    def test_bad_dates_are_explained(self):
        for bad in ("soon", "2026-13-01", "13/10/2026", "+d", "+3x", ""):
            with self.assertRaises(al.ApplyLogError, msg=bad):
                al.parse_date(bad, TODAY)


class AddTests(Case):
    def test_an_application_gets_a_follow_up_a_week_later(self):
        out = self.add("Acme", "ML Engineer", "--resume", "ai-engineer", "--source", "LinkedIn", "--url", "https://example.com/j")
        self.assertIn("added #1", out)
        self.assertIn("follow up on 2026-10-20", out)
        app = self.data()["applications"][0]
        self.assertEqual((app["status"], app["applied_on"], app["resume"], app["source"]), ("applied", "2026-10-13", "ai-engineer", "LinkedIn"))
        self.assertEqual(app["history"][0]["event"], "created")

    def test_a_wishlist_entry_has_no_dates(self):
        self.add("Globex", "Backend", "--status", "wishlist")
        app = self.data()["applications"][0]
        self.assertEqual((app["applied_on"], app["follow_up"]), (None, None))

    def test_an_earlier_application_date_moves_the_default_follow_up(self):
        self.add("Acme", "ML", "--date", "2026-10-01")
        self.assertEqual(self.data()["applications"][0]["follow_up"], "2026-10-08")

    def test_an_explicit_follow_up_wins(self):
        self.add("Acme", "ML", "--follow-up", "+3d")
        self.assertEqual(self.data()["applications"][0]["follow_up"], "2026-10-16")

    def test_ids_count_up_and_are_never_reused_after_a_removal(self):
        for name in ("A", "B", "C"):
            self.add(name, "r")
        self.run_cli("remove", "3", "--yes")
        self.add("D", "r")
        self.assertEqual([a["id"] for a in self.data()["applications"]], [1, 2, 4])

    def test_values_are_validated(self):
        for argv in (["add", "", "role"], ["add", "Co", "  "], ["add", "x" * 101, "r"], ["add", "Co", "r", "--url", "ftp://x"],
                     ["add", "Co", "r", "--resume", "v" * 41], ["add", "Co", "r", "--date", "later"]):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertTrue(err.startswith("apply-log:"), argv)
        self.assertFalse(os.path.exists(self.file))                      # nothing was saved
        self.assertEqual(self.run_cli("add", "Co", "r", "--status", "dancing")[0], 2)

    def test_extra_spaces_are_cleaned(self):
        self.add("  Acme   Corp ", " ML  Engineer ")
        app = self.data()["applications"][0]
        self.assertEqual((app["company"], app["role"]), ("Acme Corp", "ML Engineer"))


class StorageTests(Case):
    def test_the_file_and_folder_are_private(self):
        self.add()
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.file)).st_mode), 0o700)

    def test_no_temporary_files_are_left_behind(self):
        self.add()
        self.add("B", "r")
        self.assertEqual(os.listdir(os.path.dirname(self.file)), ["applications.json"])

    def test_a_failed_write_leaves_the_old_file_and_no_debris(self):
        self.add()
        before = open(self.file, encoding="utf-8").read()
        with mock.patch("apply_log.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                al.save(self.file, al.empty_data())
        self.assertEqual(open(self.file, encoding="utf-8").read(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.file)), ["applications.json"])

    def test_damaged_files_are_reported_and_never_overwritten(self):
        os.makedirs(os.path.dirname(self.file))
        bad_files = ["{not json", "[]", json.dumps({"version": 2, "applications": []}), json.dumps({"version": 1, "applications": [1]}),
                     json.dumps({"version": 1, "applications": [{"id": 1}]}),
                     json.dumps({"version": 1, "applications": [{"id": 1, "company": "a", "role": "b", "status": "nope", "history": []}]}),
                     json.dumps({"version": 1, "applications": [{"id": 1, "company": "a", "role": "b", "status": "applied", "history": [], "follow_up": "tomorrow"}]})]
        for text in bad_files:
            with open(self.file, "w", encoding="utf-8") as handle:
                handle.write(text)
            code, _, err = self.run_cli("add", "Co", "r")
            self.assertEqual(code, 1, text)
            self.assertIn("apply-log:", err)
            self.assertEqual(open(self.file, encoding="utf-8").read(), text)

    def test_duplicate_ids_are_rejected(self):
        app = {"id": 1, "company": "a", "role": "b", "status": "applied", "history": []}
        os.makedirs(os.path.dirname(self.file))
        with open(self.file, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "applications": [app, dict(app)]}, handle)
        self.assertEqual(self.run_cli("list")[0], 1)

    def test_the_environment_variable_picks_the_file(self):
        other = os.path.join(self.dir.name, "elsewhere.json")
        result = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "apply_log.py"), "add", "Co", "Role"],
                                env=dict(os.environ, APPLY_LOG_FILE=other), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(other))

    def test_unicode_survives(self):
        self.add("Київ Тех", "Інженер")
        self.assertEqual(self.data()["applications"][0]["company"], "Київ Тех")
        self.assertIn("Київ", open(self.file, encoding="utf-8").read())


class ListShowTests(Case):
    def setUp(self):
        super().setUp()
        self.add("Acme", "ML Engineer", "--resume", "ai", "--source", "LinkedIn")
        self.add("Globex", "Backend", "--resume", "general")
        self.add("Initech", "Data", "--status", "wishlist")
        self.run_cli("update", "2", "--status", "rejected")

    def test_closed_applications_are_hidden_unless_asked_for(self):
        _, out, _ = self.run_cli("list")
        self.assertIn("Acme", out)
        self.assertIn("Initech", out)
        self.assertNotIn("Globex", out)
        self.assertIn("Globex", self.run_cli("list", "--all")[1])

    def test_filters(self):
        self.assertIn("Globex", self.run_cli("list", "--status", "rejected")[1])
        self.assertNotIn("Acme", self.run_cli("list", "--status", "rejected")[1])
        self.assertIn("Acme", self.run_cli("list", "--company", "ACM")[1])
        self.assertNotIn("Initech", self.run_cli("list", "--company", "acm")[1])
        self.assertIn("Acme", self.run_cli("list", "--resume", "AI")[1])
        self.assertIn("Acme", self.run_cli("list", "--source", "LinkedIn")[1])
        self.assertNotIn("Initech", self.run_cli("list", "--source", "LinkedIn")[1])
        self.assertEqual(self.run_cli("list", "--company", "nobody")[1].strip(), "nothing to show")

    def test_json_listing_is_valid(self):
        data = json.loads(self.run_cli("list", "--all", "--json")[1])
        self.assertEqual([a["company"] for a in data], ["Acme", "Globex", "Initech"])

    def test_show_includes_the_history(self):
        _, out, _ = self.run_cli("show", "2")
        self.assertIn("#2  Globex, Backend", out)
        self.assertIn("status: rejected", out)
        self.assertIn("created", out)
        self.assertIn("from=applied to=rejected", out)
        self.assertEqual(self.run_cli("show", "99")[0], 1)


class UpdateTests(Case):
    def test_a_status_change_is_recorded_with_its_note(self):
        self.add()
        self.run_cli("update", "1", "--status", "screening", "--note", "call with Sam")
        app = self.data()["applications"][0]
        self.assertEqual(app["status"], "screening")
        change = [h for h in app["history"] if h["event"] == "status"][0]
        self.assertEqual((change["from"], change["to"], change["note"]), ("applied", "screening", "call with Sam"))
        self.assertEqual(sum(1 for h in app["history"] if h["event"] == "note"), 0)    # the note is not stored twice

    def test_moving_from_the_wishlist_to_applied_sets_the_date_and_a_follow_up(self):
        self.add("Globex", "Backend", "--status", "wishlist")
        self.run_cli("update", "1", "--status", "applied", today=date(2026, 10, 20))
        app = self.data()["applications"][0]
        self.assertEqual((app["applied_on"], app["follow_up"]), ("2026-10-20", "2026-10-27"))

    def test_closing_an_application_clears_its_follow_up_and_its_reminders(self):
        self.add()
        for status in ("rejected", "withdrawn", "ghosted", "accepted"):
            self.run_cli("update", "1", "--status", "applied")
            self.run_cli("update", "1", "--follow-up", "+1d")
            self.run_cli("update", "1", "--status", status)
            self.assertIsNone(self.data()["applications"][0]["follow_up"], status)
        self.assertEqual(self.run_cli("remind", "--within", "30")[1].strip(), "no follow-ups due in the next 30 day(s)")

    def test_follow_up_dates_can_be_set_and_cleared(self):
        self.add()
        self.run_cli("update", "1", "--follow-up", "2026-11-01")
        self.assertEqual(self.data()["applications"][0]["follow_up"], "2026-11-01")
        self.run_cli("update", "1", "--follow-up", "none")
        self.assertIsNone(self.data()["applications"][0]["follow_up"])

    def test_note_only_updates_and_field_edits_are_logged(self):
        self.add()
        self.run_cli("update", "1", "--note", "recruiter is Alex", "--resume", "general", "--company", "Acme Inc")
        app = self.data()["applications"][0]
        events = [h["event"] for h in app["history"]]
        self.assertIn("note", events)
        self.assertEqual(events.count("edited"), 2)
        self.assertEqual((app["resume"], app["company"]), ("general", "Acme Inc"))

    def test_an_unknown_id_or_bad_value_changes_nothing(self):
        self.add()
        before = open(self.file, encoding="utf-8").read()
        self.assertEqual(self.run_cli("update", "9", "--status", "offer")[0], 1)
        self.assertEqual(self.run_cli("update", "1", "--company", "")[0], 1)
        self.assertEqual(self.run_cli("update", "1", "--follow-up", "whenever")[0], 1)
        self.assertEqual(open(self.file, encoding="utf-8").read(), before)


class FollowTests(Case):
    def test_following_up_is_logged_and_the_next_one_scheduled(self):
        self.add()
        code, out, _ = self.run_cli("follow", "1", "--next-in", "5", "--note", "emailed Sam", today=date(2026, 10, 20))
        self.assertEqual(code, 0)
        self.assertIn("follow-up #1", out)
        app = self.data()["applications"][0]
        self.assertEqual(app["follow_up"], "2026-10-25")
        self.assertEqual([h for h in app["history"] if h["event"] == "followed up"][0]["note"], "emailed Sam")

    def test_the_third_follow_up_suggests_ghosted(self):
        self.add()
        outs = [self.run_cli("follow", "1")[1] for _ in range(3)]
        self.assertNotIn("ghosted", outs[1])
        self.assertIn("update 1 --status ghosted", outs[2])

    def test_a_closed_application_cannot_be_followed_up(self):
        self.add()
        self.run_cli("update", "1", "--status", "rejected")
        self.assertEqual(self.run_cli("follow", "1")[0], 1)


class RemindTests(Case):
    def test_overdue_today_and_upcoming_items_are_listed_in_date_order(self):
        self.add("Late", "r", "--follow-up", "2026-10-10")
        self.add("Now", "r", "--follow-up", "today")
        self.add("Soon", "r", "--follow-up", "+5d")
        self.add("Far", "r", "--follow-up", "+40d")
        self.add("Wish", "r", "--status", "wishlist")
        _, out, _ = self.run_cli("remind")
        self.assertIn("3 day(s) overdue", out)
        self.assertIn("today", out)
        self.assertNotIn("Soon", out)
        _, out, _ = self.run_cli("remind", "--within", "7")
        self.assertLess(out.index("Late"), out.index("Now"))
        self.assertLess(out.index("Now"), out.index("Soon"))
        self.assertIn("in 5 day(s)", out)
        self.assertNotIn("Far", out)
        self.assertNotIn("Wish", out)

    def test_remind_json_output(self):
        self.add("Late", "r", "--follow-up", "2026-10-10")
        code, out, _ = self.run_cli("remind", "--json")
        self.assertEqual(code, 0)
        items = json.loads(out)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["company"], "Late")
        self.assertEqual(items[0]["days_overdue"], 3)

    def test_nothing_due_says_so(self):
        self.assertEqual(self.run_cli("remind")[1].strip(), "no follow-ups due")


class StatsTests(Case):
    def build(self):
        self.add("A1", "r", "--resume", "ai", "--source", "referral", "--date", "2026-10-01")
        self.add("A2", "r", "--resume", "ai", "--source", "LinkedIn", "--date", "2026-10-02")
        self.add("B1", "r", "--resume", "general", "--source", "LinkedIn", "--date", "2026-10-03")
        self.add("B2", "r", "--resume", "general", "--source", "LinkedIn", "--date", "2026-10-04")
        self.add("B3", "r", "--status", "wishlist")
        self.run_cli("update", "1", "--status", "screening", today=date(2026, 10, 8))     # 7 days after applying
        self.run_cli("update", "2", "--status", "rejected", today=date(2026, 10, 5))      # 3 days
        self.run_cli("update", "3", "--status", "interview", today=date(2026, 10, 10))    # 7 days
        self.run_cli("update", "3", "--status", "rejected", today=date(2026, 10, 12))

    def test_replies_and_moves_forward_are_counted_separately(self):
        self.build()
        s = al.stats(self.data(), TODAY)
        self.assertEqual((s["total"], s["open"]), (5, 3))
        self.assertEqual(s["sent"], {"applied": 4, "replied": 3, "advanced": 2})
        self.assertEqual(s["by_resume"]["ai"], {"applied": 2, "replied": 2, "advanced": 1})
        self.assertEqual(s["by_resume"]["general"], {"applied": 2, "replied": 1, "advanced": 1})     # got an interview, then a no
        self.assertEqual(s["by_source"]["LinkedIn"]["applied"], 3)

    def test_median_days_to_first_reply(self):
        self.build()
        s = al.stats(self.data(), TODAY)
        self.assertEqual((s["median_days_to_first_reply"], s["replies_with_known_delay"]), (7, 3))

    def test_windows_count_only_sent_applications(self):
        self.build()
        s = al.stats(self.data(), date(2026, 10, 9))      # "last 7 days" is today and the six days before it: Oct 3 and 4
        self.assertEqual((s["last_7_days"], s["last_30_days"]), (2, 4))
        self.assertEqual(al.stats(self.data(), date(2026, 10, 30))["last_7_days"], 0)

    def test_small_groups_are_labelled_as_too_few_to_compare(self):
        self.build()
        out = self.run_cli("stats")[1]
        self.assertIn("too few to compare", out)
        self.assertIn("differences are noise", out)
        self.assertIn("Median days to a first reply: 7", out)

    def test_an_empty_file_has_sensible_stats(self):
        s = al.stats(al.empty_data(), TODAY)
        self.assertEqual((s["total"], s["median_days_to_first_reply"]), (0, None))
        self.assertIn("Applications: 0", al.render_stats(s))

    def test_stats_as_json(self):
        self.build()
        self.assertEqual(json.loads(self.run_cli("stats", "--json")[1])["sent"]["applied"], 4)


class ExportTests(Case):
    def test_todoist_csv_has_the_import_header_and_one_task_per_due_follow_up(self):
        self.add("Acme", "ML Engineer", "--resume", "ai", "--url", "https://example.com/j", "--follow-up", "+2d")
        self.add("Far", "r", "--follow-up", "+60d")
        self.add("Closed", "r")
        self.run_cli("update", "3", "--status", "rejected")
        rows = list(csv.reader(io.StringIO(self.run_cli("export-todoist")[1])))
        self.assertEqual(rows[0], ["TYPE", "CONTENT", "DESCRIPTION", "PRIORITY", "INDENT", "AUTHOR", "RESPONSIBLE", "DATE", "DATE_LANG", "TIMEZONE"])
        self.assertEqual(len(rows), 2)
        task = dict(zip(rows[0], rows[1]))
        self.assertEqual((task["TYPE"], task["CONTENT"], task["DATE"], task["DATE_LANG"]), ("task", "Follow up: Acme, ML Engineer", "2026-10-15", "en"))
        self.assertIn("apply-log #1 | status applied | resume ai | https://example.com/j", task["DESCRIPTION"])

    def test_the_window_can_be_widened_and_written_to_a_file(self):
        self.add("Far", "r", "--follow-up", "+60d")
        self.assertEqual(len(self.run_cli("export-todoist")[1].splitlines()), 1)
        target = os.path.join(self.dir.name, "t.csv")
        code, out, _ = self.run_cli("export-todoist", "--within", "90", "--out", target)
        self.assertEqual(code, 0)
        self.assertIn("wrote", out)
        self.assertEqual(len(open(target, encoding="utf-8").read().splitlines()), 2)

    def test_spreadsheet_formulas_in_names_are_neutralised(self):
        self.add("=HYPERLINK(\"http://x\")", "+cmd", "--follow-up", "today")
        todoist = self.run_cli("export-todoist")[1]
        self.assertNotIn(",=HYPERLINK", todoist)
        full = self.run_cli("export")[1]
        for line in full.splitlines()[1:]:
            self.assertNotIn(",=", line)
            self.assertNotIn(",+cmd", line)
        self.assertIn("'=HYPERLINK", full)

    def test_full_exports(self):
        self.add("Acme", "ML", "--note", "a, \"quoted\" note")
        self.add("Closed", "Dev")
        self.run_cli("update", "2", "--status", "rejected")
        rows = list(csv.DictReader(io.StringIO(self.run_cli("export")[1])))
        self.assertEqual((rows[0]["company"], rows[0]["notes"]), ("Acme", 'a, "quoted" note'))
        self.assertEqual(len(rows), 2)
        open_rows = list(csv.DictReader(io.StringIO(self.run_cli("export", "--open-only")[1])))
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["company"], "Acme")
        self.assertEqual(json.loads(self.run_cli("export", "--format", "json")[1])[0]["company"], "Acme")


class RemoveAndCheckTests(Case):
    def test_removing_needs_confirmation(self):
        self.add()
        code, _, err = self.run_cli("remove", "1")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(len(self.data()["applications"]), 1)
        self.assertEqual(self.run_cli("remove", "1", "--yes")[0], 0)
        self.assertEqual(self.data()["applications"], [])

    def test_check_points_out_missing_follow_ups_and_stale_entries(self):
        self.add("Quiet", "r", today=date(2026, 8, 1))
        self.add("NoDate", "r")
        self.run_cli("update", "2", "--follow-up", "none")
        out = self.run_cli("check")[1]
        self.assertIn("file is valid: 2 application(s)", out)
        self.assertIn("#2 NoDate: applied but no follow-up date", out)
        self.assertIn("#1 Quiet: no update since 2026-08-01", out)
        self.assertNotIn("#2 NoDate: no update", out)


class TodayOverrideTests(Case):
    def test_apply_log_today_makes_runs_reproducible(self):
        script = os.path.join(os.path.dirname(__file__), "..", "apply_log.py")
        env = dict(os.environ, APPLY_LOG_TODAY="2026-03-02")
        result = subprocess.run([sys.executable, script, "--file", self.file, "add", "Co", "Role"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("follow up on 2026-03-09", result.stdout)
        bad = subprocess.run([sys.executable, script, "--file", self.file, "list"], env=dict(os.environ, APPLY_LOG_TODAY="soon"), capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("APPLY_LOG_TODAY", bad.stderr)


class ProcessTests(unittest.TestCase):
    def test_version_and_usage_errors(self):
        script = os.path.join(os.path.dirname(__file__), "..", "apply_log.py")
        result = subprocess.run([sys.executable, script, "--version"], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"apply-log {al.__version__}"))
        self.assertEqual(subprocess.run([sys.executable, script], capture_output=True).returncode, 2)

    def test_the_default_location_is_in_the_home_folder_not_the_repository(self):
        self.assertTrue(al.DEFAULT_FILE.startswith(os.path.expanduser("~")))
        self.assertNotIn("apply-log" + os.sep + "tests", al.DEFAULT_FILE)


if __name__ == "__main__":
    unittest.main()


class ClosedFollowUpTests(unittest.TestCase):
    def test_a_closed_application_cannot_be_given_a_follow_up_date_and_is_left_untouched(self):
        import datetime as dt

        today = dt.date(2026, 10, 18)
        data = {"next_id": 1, "applications": []}
        al.add_application(data, today, "Acme", "SWE")
        with self.assertRaises(al.ApplyLogError):
            al.update_application(data, today, 1, status="rejected", follow_up=dt.date(2026, 11, 1))
        self.assertEqual(data["applications"][0]["status"], "applied")
        al.update_application(data, today, 1, status="rejected")
        self.assertIsNone(data["applications"][0]["follow_up"])


class AppliedOnHistoryTests(unittest.TestCase):
    def test_changing_applied_on_is_recorded_in_history(self):
        data = al.empty_data()
        app = al.add_application(data, TODAY, "Acme", "ML Engineer", applied_on=date(2026, 10, 1))
        before = len(app["history"])
        al.update_application(data, TODAY, app["id"], applied_on=date(2026, 10, 5))
        self.assertEqual(app["applied_on"], "2026-10-05")
        self.assertEqual(len(app["history"]), before + 1)
        event = app["history"][-1]
        self.assertEqual(event["event"], "edited")
        self.assertEqual(event["field"], "applied_on")
        self.assertEqual(event["to"], "2026-10-05")
        self.assertEqual(event["on"], TODAY.isoformat())

    def test_unchanged_applied_on_adds_no_history(self):
        data = al.empty_data()
        app = al.add_application(data, TODAY, "Acme", "ML Engineer", applied_on=date(2026, 10, 1))
        before = list(app["history"])
        al.update_application(data, TODAY, app["id"], applied_on=date(2026, 10, 1))
        self.assertEqual(app["applied_on"], "2026-10-01")
        self.assertEqual(app["history"], before)

    def test_applied_on_edit_via_cli_shows_in_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "applications.json")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(al.main(["--file", path, "add", "Acme", "ML Engineer", "--date", "2026-10-01"], today=TODAY), 0)
                self.assertEqual(al.main(["--file", path, "update", "1", "--applied-on", "2026-10-03"], today=TODAY), 0)
            app = al.load(path)["applications"][0]
            self.assertIn({"on": TODAY.isoformat(), "event": "edited", "field": "applied_on", "to": "2026-10-03"}, app["history"])
