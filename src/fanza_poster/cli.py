"""コマンドライン: python -m fanza_poster <コマンド>"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from .config import ConfigError, load_settings

log = logging.getLogger("fanza_poster")


def _ask(prompt: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        print("(対話できない環境のため実行しません)")
        return False
    try:
        return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _lock(app):
    from .lockfile import RunLock

    return RunLock(app.settings.data_dir / "run.lock")


# ------------------------------------------------------------------ コマンド
def cmd_login(app, args) -> int:
    from .post.base import DetectionError

    print("ログイン用のブラウザを開きます。")
    print("ブラウザで X にログインしてください(ID・パスワード・確認コードの入力はご自身で行ってください)。")
    with _lock(app):
        with app.new_poster() as poster:
            poster.open_for_login()
            try:
                input("ログインが終わったら、この画面で Enter キーを押してください...")
            except EOFError:
                pass
            try:
                handle = poster.check_session()
            except DetectionError as e:
                print(f"[NG] ログイン状態を確認できませんでした: {e.detail}")
                return 1
    print(f"[OK] @{handle} でログインしています。ログイン状態はプロファイルに保存されました。")
    if not app.cfg.post.x_handle:
        print(f"ヒント: config.yaml の post.x_handle に {handle} を設定すると、別アカウントでの誤投稿を防げます。")
    return 0


def cmd_selfcheck(app, args) -> int:
    from .post.base import DetectionError
    from .post.detect import KIND_JA
    from .safety import Safety
    from .schedule.phase import auto_start_date, compute_phase

    results: list[tuple[str, bool, str]] = []
    cfg = app.cfg
    safety = Safety(app)

    ops = app.ops_date()
    phase = compute_phase(ops, cfg, safety.ramp_frozen())
    results.append(("設定ファイル", True, f"{app.settings.config_path}"))
    results.append((
        "運用フェーズ", True,
        f"本日({ops}): {phase.label} / 予定 {phase.target_posts} 件 / 自動投稿の開始日 {auto_start_date(cfg)}"
        + (" / ドライランモード" if cfg.dry_run else ""),
    ))
    results.append(("収集対象", True, f"フロア {', '.join(cfg.collect.floors)} / 割引率 {cfg.collect.min_discount_rate}% 以上"))

    missing = app.settings.secrets.missing()
    placeholders = app.settings.secrets.placeholders()
    if missing:
        results.append((".env", False, f"未設定: {', '.join(missing)}"))
    elif placeholders:
        results.append((".env", False, f"雛形の値のまま: {', '.join(placeholders)}"))
    else:
        results.append((".env", True, "OK"))

    try:
        mapping = {style: app.settings.affiliate_id_for(style) for style in cfg.tracking.affiliate_ids}
        warns = app.settings.tracking_warnings()
        msg = " / ".join(f"{k}={v}" for k, v in mapping.items())
        results.append(("計測用アフィリエイトID", not warns, msg + ("" if not warns else " / " + " ".join(warns))))
    except ConfigError as e:
        results.append(("計測用アフィリエイトID", False, str(e)))

    try:
        app.db.set_state("selfcheck_at", datetime.now().isoformat(timespec="seconds"))
        results.append(("データベース", True, str(app.settings.db_path)))
    except Exception as e:  # noqa: BLE001
        results.append(("データベース", False, str(e)))

    if not args.skip_api:
        try:
            dmm = app.dmm()
            counts = []
            for floor in cfg.collect.floors:
                items = dmm.item_list(floor, hits=1)
                counts.append(f"{floor}: 応答あり({len(items)}件)")
            results.append(("DMMアフィリエイトAPI", True, " / ".join(counts)))
        except Exception as e:  # noqa: BLE001
            results.append(("DMMアフィリエイトAPI", False, str(e)))
        try:
            name = app.writer().check_model()
            results.append(("Anthropic API", True, f"モデル {cfg.generation.model}({name})"))
        except Exception as e:  # noqa: BLE001
            results.append(("Anthropic API", False, str(e)))

    if not args.skip_browser:
        try:
            with _lock(app):
                with app.new_poster() as poster:
                    handle = poster.check_session()
            results.append(("Xのログイン状態", True, f"@{handle} でログイン中(投稿はしていません)"))
        except DetectionError as e:
            results.append(("Xのログイン状態", False, f"{KIND_JA.get(e.kind, e.kind)}: {e.detail}"))
        except Exception as e:  # noqa: BLE001
            results.append(("Xのログイン状態", False, f"ブラウザを起動できません: {e}"))

    if args.notify_test:
        app.notify("テスト通知", "FANZA自動投稿ツールの通知テストです。")
        results.append(("Windows通知", True, "テスト通知を送りました(表示されたか確認してください)"))

    info = safety.halted()
    results.append(("停止状態", info is None, "停止していません" if info is None else f"停止中: {info['reason']}"))
    frozen = safety.ramp_frozen()
    if frozen is not None:
        results.append(("件数の引き上げ", True, f"警告検知により {frozen} 件で据え置き中(unfreeze-ramp で解除)"))

    print()
    print("=== selfcheck 結果 ===")
    for name, ok, msg in results:
        print(f"[{'OK' if ok else 'NG'}] {name}: {msg}")
    ng = [r for r in results if not r[1]]
    print()
    print("すべて OK です。" if not ng else f"NG が {len(ng)} 件あります。上の内容を確認してください。")
    return 0 if not ng else 1


def cmd_dry_run(app, args) -> int:
    """投稿せず、文面のサンプルを生成して output/dryrun/ に出力する(DBの状態は変えない)。"""
    from .collect.collector import collect
    from .generate.summary import PERIOD_WEEKLY
    from .generate.writer import GenerationError
    from .models import KIND_SINGLE, KIND_SUMMARY, Slot
    from .patterns import DIM_GENRE, DIM_SUMMARY, DIM_TONE, single_pattern_id, summary_pattern_id
    from .post.dryrun import render_slot
    from .schedule.planner import Planner
    from .schedule.slots import band_of
    from .timeutil import iso, minutes_since_midnight

    with _lock(app):
        ops = app.ops_date()
        if not args.no_collect:
            print("DMMアフィリエイトAPIからセール商品を取得しています...")
            stats = collect(app, ops)
            print(f"  取得 {stats.unique} 件 → 割引率{app.cfg.collect.min_discount_rate}%以上かつ除外に当たらないもの {stats.kept} 件"
                  f"(除外 {stats.excluded} 件)")
        planner = Planner(app)
        now = app.now()
        band = band_of(minutes_since_midnight(now, ops, app.tz), app.cfg.schedule.bands)
        pools = {g: planner.candidates(g, ops, set()) for g in planner.patterns.enabled(DIM_GENRE)}
        picked = []
        while len(picked) < args.count and any(pools.values()):
            for g in list(pools):
                if pools[g] and len(picked) < args.count:
                    picked.append(pools[g].pop(0))
        tones = planner.patterns.enabled(DIM_TONE)
        slots: list[Slot] = []
        for i, p in enumerate(picked):
            tone = tones[i % len(tones)]
            print(f"文面を生成中: {p.content_id}(トーン {tone})")
            try:
                text = planner.write_single(p, tone)
            except GenerationError as e:
                print(f"  生成できませんでした: {e}")
                continue
            af = app.settings.affiliate_id_for(tone)
            slots.append(Slot(
                id=None, ops_date=ops.isoformat(), seq=len(slots) + 1, scheduled_at=iso(now), kind=KIND_SINGLE,
                band=band, style=tone, genre=p.genre, pattern_id=single_pattern_id(tone, band, p.genre),
                content_ids=[p.content_id], text=text, reply_text=planner.reply_for(p, af),
                image_urls=planner.images_for(p), affiliate_id=af, dry_run=True,
            ))
        period = PERIOD_WEEKLY if args.weekly else planner.period_for(ops)
        top = planner.ranking(ops, period)[: app.cfg.summary.top_n]
        if len(top) >= app.cfg.summary.min_items:
            fmt = planner.patterns.enabled(DIM_SUMMARY)[0]
            af = app.settings.affiliate_id_for(fmt)
            print(f"まとめ投稿を生成中(形式 {fmt})")
            labels = planner.labels_for(top)
            text, reply = planner.render_summary(fmt, period, top, labels, af)
            slots.append(Slot(
                id=None, ops_date=ops.isoformat(), seq=len(slots) + 1, scheduled_at=iso(now), kind=KIND_SUMMARY,
                band=band, style=fmt, period=period, pattern_id=summary_pattern_id(fmt, band, period),
                content_ids=[p.content_id for p in top], text=text, reply_text=reply,
                image_urls=planner.images_for(top[0]), affiliate_id=af, dry_run=True,
            ))
        else:
            print(f"まとめ投稿の候補が {len(top)} 件のため、まとめ投稿は作りませんでした。")

    if not slots:
        print("出力できる文面がありませんでした(候補がない、または生成に失敗)。")
        return 1
    out = app.settings.output_dir / "dryrun" / f"sample_{now:%Y%m%d_%H%M%S}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(render_slot(app, s, i + 1, len(slots)) for i, s in enumerate(slots))
    out.write_text("ドライラン(サンプル)。投稿はしていません。\n\n" + body + "\n", encoding="utf-8-sig")
    print()
    print(body)
    print(f"\n出力しました: {out}")
    return 0


def cmd_tick(app, args) -> int:
    from .lockfile import LockBusy
    from .runner import Runner

    try:
        with _lock(app):
            return Runner(app).tick(force_dry_run=args.dry_run)
    except LockBusy as e:
        log.info("%s(今回は何もしません)", e)
        return 0


def cmd_collect(app, args) -> int:
    from .collect.collector import collect

    with _lock(app):
        stats = collect(app, app.ops_date())
    print(f"取得 {stats.fetched} 件 / 候補として保存 {stats.kept} 件 / 除外 {stats.excluded} 件")
    return 0


def cmd_plan(app, args) -> int:
    from .collect.collector import ensure_collected
    from .safety import Safety
    from .schedule.phase import compute_phase
    from .schedule.planner import Planner

    with _lock(app):
        ops = app.ops_date()
        phase = compute_phase(ops, app.cfg, Safety(app).ramp_frozen())
        ensure_collected(app, ops)
        created = Planner(app).ensure_plan(ops, app.now(), phase, force_dry_run=args.dry_run)
    print("計画を作成しました。" if created else "本日の計画は作成済みです。")
    return cmd_status(app, args)


def cmd_status(app, args) -> int:
    from .models import PRODUCT_STATUS_JA, RESULT_JA
    from .patterns import DIM_JA, Patterns
    from .postlog import kind_label
    from .safety import Safety
    from .schedule.phase import compute_phase
    from .timeutil import parse_iso

    safety = Safety(app)
    ops = app.ops_date()
    phase = compute_phase(ops, app.cfg, safety.ramp_frozen())
    print(f"運用日: {ops}  {phase.label}  予定件数: {phase.target_posts}  {phase.note}")
    print(f"本日の投稿数(上限計算用): {app.db.counted_posts(ops)} / 上限 {app.cfg.schedule.daily_hard_limit}")
    info = safety.halted()
    print(f"停止状態: {'停止中 - ' + info['reason'] + ' (' + info['at'] + ')' if info else '稼働中'}")
    print(f"連続失敗: {safety.consecutive_failures()} / {app.cfg.safety.max_consecutive_failures}")
    frozen = safety.ramp_frozen()
    if frozen is not None:
        print(f"件数の引き上げ: {frozen} 件で据え置き中")
    plan = app.db.get_plan(ops)
    if plan is None:
        print("本日の計画: まだありません")
    else:
        print(f"本日の計画: {plan['target_posts']} 件 {'(ドライラン)' if plan['dry_run'] else ''} {plan['note'] or ''}")
        for s in app.db.slots_for(ops):
            at = parse_iso(s.scheduled_at).strftime("%H:%M")
            state = RESULT_JA.get(s.result, s.result) if s.result else "待機"
            print(f"  {at} {kind_label(s):10} {s.pattern_id:14} {state:12} {','.join(s.content_ids)[:40]} {s.error}")
    counts = app.db.product_counts()
    print("商品キュー: " + " / ".join(f"{PRODUCT_STATUS_JA.get(k, k)} {v}" for k, v in sorted(counts.items())))
    disabled = Patterns(app.cfg, app.db).disabled()
    if disabled:
        print("無効化中のパターン: " + ", ".join(f"{DIM_JA.get(d, d)}:{c}" for d, c, _, _ in disabled))
    return 0


def cmd_resume(app, args) -> int:
    from .safety import Safety

    safety = Safety(app)
    info = safety.halted()
    if info is None:
        print("停止していません。")
        return 0
    print(f"停止理由: {info['reason']}({info['at']})")
    print("X の画面で、警告・制限・本人確認などが解消していることを確認してから再開してください。")
    if not args.yes and not _ask("停止を解除しますか?"):
        print("解除しませんでした。")
        return 1
    safety.resume()
    print("停止を解除しました。")
    frozen = safety.ramp_frozen()
    if frozen is not None:
        print(f"1日の件数は {frozen} 件で据え置いたままです。引き上げを再開する場合は unfreeze-ramp を実行してください。")
    return 0


def cmd_unfreeze(app, args) -> int:
    from .safety import Safety

    safety = Safety(app)
    frozen = safety.ramp_frozen()
    if frozen is None:
        print("件数の引き上げは停止していません。")
        return 0
    if not args.yes and not _ask(f"現在 {frozen} 件で据え置き中です。週次スケジュールどおりの件数に戻しますか?"):
        return 1
    safety.unfreeze_ramp()
    print("据え置きを解除しました。")
    return 0


def cmd_report(app, args) -> int:
    from .analytics.report import write_report

    try:
        md_path, csv_path, md = write_report(app, args.week)
    except ValueError as e:
        print(f"集計できません: {e}")
        return 1
    print(md)
    print(f"\n出力しました: {md_path} / {csv_path}")
    return 0


def cmd_review(app, args) -> int:
    from .analytics.report import Dataset, default_week, fmt_num
    from .analytics.review import find_candidates
    from .patterns import DIM_JA, DIMENSIONS, Patterns

    patterns = Patterns(app.cfg, app.db)
    if args.enable or args.disable:
        dim, code = args.enable or args.disable
        if dim not in DIMENSIONS:
            print(f"区分は {', '.join(DIMENSIONS)} のいずれかです")
            return 1
        enable = bool(args.enable)
        verb = "有効" if enable else "無効"
        if not args.yes and not _ask(f"{DIM_JA[dim]} {code}({patterns.name(dim, code)})を{verb}にしますか?"):
            return 1
        try:
            patterns.set_enabled(dim, code, enable, note="手動で変更")
        except ValueError as e:
            print(e)
            return 1
        print(f"{DIM_JA[dim]} {code} を{verb}にしました。")
        return 0

    print("現在の状態:")
    for dim in DIMENSIONS:
        enabled = patterns.enabled(dim)
        items = [f"{c}({patterns.name(dim, c)}){'' if c in enabled else '[無効]'}" for c in patterns.codes(dim)]
        print(f"  {DIM_JA[dim]}: {' / '.join(items)}")
    if args.list:
        return 0

    try:
        week = args.week or default_week(app)
    except ValueError as e:
        print(e)
        return 1
    ds = Dataset(app)
    candidates, notes = find_candidates(ds, week)
    print(f"\n第{week}週までの集計による無効化候補:")
    for n in notes:
        print(f"  - {n}")
    if not candidates:
        print("  候補はありません。")
        return 0
    changed = 0
    for c in candidates:
        print(
            f"\n{DIM_JA[c.dim]} {c.code}({c.name}): 投稿 {c.posts} 件 / 1投稿あたりクリック {fmt_num(c.cpp)}"
            f"(区分の平均 {fmt_num(c.mean_cpp)})"
        )
        if _ask("このパターンを無効化しますか?"):
            try:
                patterns.set_enabled(c.dim, c.code, False, note=f"第{week}週の集計で無効化(1投稿あたり {c.cpp:.2f})")
                changed += 1
                print("  無効化しました。")
            except ValueError as e:
                print(f"  {e}")
        else:
            print("  そのままにしました。")
    print(f"\n{changed} 件を無効化しました。元に戻すには: fp.bat review --enable 区分 コード")
    return 0


# ------------------------------------------------------------------ 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fanza-poster", description="FANZA セール情報の X 自動投稿ツール")
    p.add_argument("--config", help="設定ファイルのパス(既定: config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="詳細なログを出す")
    sub = p.add_subparsers(dest="command", required=True, metavar="コマンド")

    sub.add_parser("login", help="X に手動ログインし、ログイン状態をプロファイルに保存する")

    sc = sub.add_parser("selfcheck", help="設定・API・ログイン状態を確認する(投稿はしない)")
    sc.add_argument("--skip-browser", action="store_true", help="ブラウザでのログイン確認を省く")
    sc.add_argument("--skip-api", action="store_true", help="DMM / Anthropic API の確認を省く")
    sc.add_argument("--notify-test", action="store_true", help="Windows通知のテストも行う")

    dr = sub.add_parser("dry-run", help="投稿せず、文面のサンプルを生成して出力する")
    dr.add_argument("--count", type=int, default=3, help="個別投稿のサンプル数(既定: 3)")
    dr.add_argument("--no-collect", action="store_true", help="APIから取得し直さず、保存済みの候補を使う")
    dr.add_argument("--weekly", action="store_true", help="まとめ投稿を週間TOP5で作る")

    tk = sub.add_parser("tick", help="定期実行の本体(タスクスケジューラから呼ぶ)")
    tk.add_argument("--dry-run", action="store_true", help="今回は投稿せず出力だけにする")

    sub.add_parser("collect", help="セール商品を取得する")
    pl = sub.add_parser("plan", help="本日の投稿計画を作る(作成済みなら表示のみ)")
    pl.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="本日の計画・停止状態などを表示する")

    rs = sub.add_parser("resume", help="停止を解除する")
    rs.add_argument("--yes", action="store_true", help="確認なしで解除する")
    uf = sub.add_parser("unfreeze-ramp", help="警告検知による件数の据え置きを解除する")
    uf.add_argument("--yes", action="store_true")

    rp = sub.add_parser("report", help="週次集計を出力する")
    rp.add_argument("--week", type=int, help="集計する週(既定: 直近で終わった週)")

    rv = sub.add_parser("review", help="下位パターンの無効化候補を確認し、承認したものを無効化する")
    rv.add_argument("--week", type=int, help="判定の基準にする週(既定: 直近で終わった週)")
    rv.add_argument("--list", action="store_true", help="現在の有効/無効を表示するだけ")
    rv.add_argument("--enable", nargs=2, metavar=("区分", "コード"), help="例: --enable tone TB")
    rv.add_argument("--disable", nargs=2, metavar=("区分", "コード"), help="例: --disable genre OTH")
    rv.add_argument("--yes", action="store_true")
    return p


COMMANDS = {
    "login": cmd_login,
    "selfcheck": cmd_selfcheck,
    "dry-run": cmd_dry_run,
    "tick": cmd_tick,
    "collect": cmd_collect,
    "plan": cmd_plan,
    "status": cmd_status,
    "resume": cmd_resume,
    "unfreeze-ramp": cmd_unfreeze,
    "report": cmd_report,
    "review": cmd_review,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    args = build_parser().parse_args(argv)

    from .app import App
    from .lockfile import LockBusy
    from .logging_setup import setup_logging

    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1
    setup_logging(settings.log_dir, args.verbose)
    app = App.create(settings)
    try:
        return COMMANDS[args.command](app, args)
    except ConfigError as e:
        log.error("%s", e)
        return 1
    except LockBusy as e:
        print(e)
        return 1
    except KeyboardInterrupt:
        print("中断しました。")
        return 1
    except Exception as e:  # noqa: BLE001
        log.exception("予期しないエラー: %s", e)
        if args.command == "tick":
            try:
                from .safety import Safety

                Safety(app).notify_once_per_day("unexpected", "自動投稿ツールでエラー", str(e)[:200])
            except Exception:  # noqa: BLE001
                pass
        return 1
    finally:
        app.db.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

