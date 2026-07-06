"""スラッシュコマンドハンドラ — /taka-ma-* 各コマンドの受け口。

タスク投入（会話キュー）・状態表示・緊急停止/復旧・ユーザー管理・モデル管理などを担う。
各ハンドラは ack() 直後に authorize() で必要ロールをゲートしてから処理する
（RBAC 要件は運用書「コマンドごとのロール要件」）。

構築手順書: docs/procedures/03-slack-bot.md
運用情報:   docs/operations/u-zu/slack-bot.md
"""

import logging
import os
import re
import shlex
import subprocess

from templates.status_block import build_status_blocks
from templates.log_block import build_log_blocks
from services.conversation_queue import enqueue_conversation_message
from services.approval_store import resolve_approval
from services.control_store import enqueue_control, COMMAND_STOP_OLLAMA
from services.role_check import (
    authorize, can_manage_user, get_role, role_denied_message,
)
from services import user_store
from services import model_store
from services import model_ops
from services.model_args import parse_model_opts, build_model_conf

logger = logging.getLogger("u-zu.commands")

# Slack がスラッシュコマンド本文に埋め込むユーザーメンション（`<@U123>` / `<@U123|name>`）。
_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")

# launchd サービス（sa-ru / ya-ta は u-zu と同居）。stop / start / モデル反映時の再起動で共用。
_CORE_SERVICES = [
    ("com.taka-ma.sa-ru", "com.taka-ma.sa-ru.plist"),
    ("com.taka-ma.ya-ta", "com.taka-ma.ya-ta.plist"),
]

def _launchctl(action: str, plist_name: str) -> bool:
    """~/Library/LaunchAgents 配下の plist を load / unload する。成功なら True。"""
    plist = os.path.join(os.path.expanduser("~/Library/LaunchAgents"), plist_name)
    try:
        subprocess.run(
            ["launchctl", action, plist],
            check=True, capture_output=True, text=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _result_line(ok: bool, name: str, verb: str) -> str:
    """launchctl 操作結果を 1 行に整形する。失敗時は語尾に「失敗」を付け、
    絵文字だけでなくテキストでも成否が読めるようにする（緊急停止時の誤読防止）。"""
    return f"{':white_check_mark:' if ok else ':x:'} `{name}` {verb}{'' if ok else '失敗'}"


def _restart_core_services() -> list[str]:
    """sa-ru / ya-ta を unload→load で再起動し、結果行を返す。

    ya-ta.yaml の models/routing は両サービスとも起動時に読むため
    （orchestrator/__main__.py / ai_gateway.classifier）、モデル反映には両方の再起動が要る。
    """
    results = []
    for name, plist in _CORE_SERVICES:
        _launchctl("unload", plist)            # 停止（失敗＝未起動でも load で復旧するので無視）
        ok = _launchctl("load", plist)         # 起動
        results.append(_result_line(ok, name, "再起動"))
    return results


def _parse_user_mention(token: str):
    """メンショントークンから (user_id, name) を取り出す。生の `U123` も許容。"""
    m = _MENTION_RE.match(token)
    if m:
        return m.group(1), (m.group(2) or m.group(1))
    if re.fullmatch(r"[UW][A-Z0-9]+", token):
        return token, token
    return None, None


def _model_usage() -> str:
    """/taka-ma-model の使い方を 1 メッセージにまとめた文字列を返す（引数不正時に提示）。"""
    return (
        ":warning: 使い方:\n"
        "`/taka-ma-model add <名前> --full-name <full> --vendor <v> "
        "--methods <m1,m2> [--model-flag <f>] [--command <cli>] "
        "[--capabilities <c1,c2>] [--description <d>]`\n"
        "`/taka-ma-model add <名前> --model-id <id> --methods <m> [--command ollama] ...`（ローカル）\n"
        "`/taka-ma-model update <名前> --<field> <値> ...`\n"
        "`/taka-ma-model remove|install|uninstall <名前>` / `/taka-ma-model list`"
    )


def _model_list(say):
    """登録済みモデルを ya-ta.yaml から読み、キー順に整形して Slack へ返す。"""
    try:
        models = model_store.load_models()
    except (ValueError, OSError) as e:
        # ValueError=YAML 解析失敗（model_store が YAMLError を正規化）、OSError=読取失敗。
        # 捕捉しないと ack 後にハンドラが落ち、ユーザーへ何も返らない（M9）。
        say(f":x: {e}")
        return
    if not models:
        say(":information_source: 登録モデルはありません")
        return
    lines = []
    for key, conf in sorted(models.items()):
        bits = [f"*{key}*", f"`{conf.get('full_name', '?')}`",
                conf.get("type", "?")]
        if conf.get("model_id"):
            bits.append(conf["model_id"])
        lines.append("• " + " — ".join(bits))
    say(":robot_face: 登録モデル\n" + "\n".join(lines))


def _model_add_or_update(sub: str, tokens: list[str], say):
    """add / update の本体 — 先頭トークンをキー、残りを `--flag 値` として ya-ta.yaml を編集する。

    yaml 編集のみで、ollama pull やサービス再起動はしない（反映は install）。
    """
    if not tokens:
        say(_model_usage())
        return
    key, opt_tokens = tokens[0], tokens[1:]
    try:
        fields = parse_model_opts(opt_tokens)
        if sub == "add":
            model_store.add_model(key, build_model_conf(fields))
            say(f":white_check_mark: 登録: `{key}` を ya-ta.yaml に追加しました"
                "（反映は `install`）")
        else:
            model_store.update_model(key, fields)
            say(f":white_check_mark: 更新: `{key}` を変更しました（反映は `install`）")
    except (ValueError, OSError) as e:
        # ValueError=入力/検証エラー、OSError=yaml 原子的書込（os.replace 等）の失敗。
        # 後者を取りこぼすと ack 後にハンドラが落ち、ユーザーへ何も返らない。
        say(f":x: {e}")


def _model_remove(tokens: list[str], say):
    """remove の本体 — ya-ta.yaml の登録だけ解除する（worker の実体削除は uninstall）。"""
    if not tokens:
        say(_model_usage())
        return
    try:
        model_store.remove_model(tokens[0])
        say(f":white_check_mark: 削除: `{tokens[0]}` を登録解除しました（反映は `uninstall`）")
    except (ValueError, OSError) as e:
        say(f":x: {e}")


def _model_install_or_uninstall(sub: str, tokens: list[str], say):
    """install / uninstall の本体 — 実体の DL/削除とサービス再起動まで行う。

    install:   type:local なら worker で ollama pull + 来歴記録し、sa-ru/ya-ta を再起動して反映。
    uninstall: 先に worker の実体を削除し（失敗時は yaml を触らず中断）、yaml 登録を解除して再起動。
    実体削除→yaml の順にするのは「yaml だけ消えて再起動されない不整合」を避けるため。
    """
    if not tokens:
        say(_model_usage())
        return
    key = tokens[0]
    try:
        conf = model_store.get_model(key)
    except (ValueError, OSError) as e:
        # get_model→load_models は壊れた ya-ta.yaml で ValueError（YAMLError 正規化）を投げ得る。
        # ここで捕捉しないと ack 後にハンドラが落ち、install/uninstall が無応答になる（M9）。
        say(f":x: {e}")
        return
    if conf is None:
        if sub == "install":
            say(f":x: 未登録: `{key}`（先に `add` してください）")
            return
        # uninstall: yaml に無くても worker 側の取りこぼし削除は許容しないため未登録扱い。
        say(f":x: 未登録: `{key}`")
        return

    is_local = conf.get("type") == "local"
    model_id = conf.get("model_id", "")
    # ローカルモデルの install は実体 DL（ollama pull）が本体。model_id が無いと
    # pull を丸ごとスキップしたまま「導入しました」と偽成功を返す（M8）。事前に弾く。
    if sub == "install" and is_local and not model_id:
        say(f":x: `{key}` は type:local ですが model_id 未設定です"
            f"（`/taka-ma-model update {key} --model-id <id>` の後に install してください）")
        return
    # _ssh は subprocess.TimeoutExpired（RuntimeError/ValueError ではない）を投げ得るため
    # 併せて捕捉する。捕まえ損ねると ack 後にハンドラが落ち、ユーザーへ何も返らない。
    try:
        if sub == "install":
            if is_local and model_id:
                model_ops.pull_model(model_id)       # worker で実体 DL
                model_ops.record_manifest(model_id)  # 来歴記録（best-effort）
        else:  # uninstall
            # worker 側の実体削除を先に行い、失敗時は yaml を変更せず中断する
            # （install の pull→反映 と対称。yaml だけ消えて再起動されない不整合を防ぐ）。
            # 実体が既に無い場合は `/taka-ma-model remove`（yaml のみ）を使う。
            if is_local and model_id:
                model_ops.remove_worker_model(model_id)   # worker から実体削除
            try:
                model_store.remove_model(key)             # 登録解除
            except (ValueError, OSError) as e:
                # 実体は削除済みだが yaml 削除に失敗（並行編集 ValueError / 書込 OSError）。
                # yaml だけ取り残されると再起動後に存在しないモデルへルーティングされるため、
                # 手動解除を明示誘導する（ここで握らないと worker/yaml が黙って乖離する）。
                say(f":warning: worker からの実体削除は完了しましたが、ya-ta.yaml の登録解除に失敗: {e}\n"
                    f"`/taka-ma-model remove {key}` で登録のみ解除してください")
                return
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
        # OSError は install の yaml 書込・SSH 失敗など。ack 後の未捕捉例外=無応答を防ぐ。
        say(f":x: {sub} 失敗: {e}")
        return

    results = _restart_core_services()
    verb = "導入" if sub == "install" else "撤去"
    say(f":rocket: `{key}` を{verb}しました\n" + "\n".join(results))


def register_commands(app):
    """/taka-ma-* スラッシュコマンドのハンドラを Bolt App に登録する。"""

    @app.command("/taka-ma-task")
    def handle_task(ack, say, command):
        """/taka-ma-task — 本文を会話キューへ 1 ターン投入する（即タスク化はしない、§8.3 (A)）。"""
        ack()
        if not authorize(command["user_id"], "user", say):
            return
        task_text = command["text"]
        logger.info("/taka-ma-task 受信: %s", task_text)
        # 直実行をやめ、会話キューへ 1 ターン投入する。意図が明確なら sa-ru の脳が
        # その場で締めて着手確認を提示し、曖昧なら会話を続ける（§8.3 (A)）。
        enqueue_conversation_message(
            "slack_command", task_text,
            user_id=command["user_id"],
            team_id=command.get("team_id", ""),
            channel_id=command["channel_id"],
        )
        say(f":speech_balloon: 受け取りました: `{task_text}`")

    @app.command("/taka-ma-go")
    def handle_go(ack, say, command):
        """/taka-ma-go — LLM 意図判定を待たず直近会話を要約させ着手確認へ進む（force_ready、§8.3 (B)）。"""
        ack()
        if not authorize(command["user_id"], "user", say):
            return
        text = command["text"]
        logger.info("/taka-ma-go 受信: %s", text)
        # 定型命令経路の明示エスケープ。LLM 意図判定を待たず、直近会話を要約させて
        # 着手確認へ進める（force_ready=True）。締めワードの言い回しに依存しない確実な締め。
        enqueue_conversation_message(
            "slack_go", text,
            user_id=command["user_id"],
            team_id=command.get("team_id", ""),
            channel_id=command["channel_id"],
            force_ready=True,
        )
        say(":rocket: これまでの会話で着手します。要約を確認してください。")

    @app.command("/taka-ma-status")
    def handle_status(ack, say, command):
        """/taka-ma-status — 各サービス・ollama の稼働状況を Block Kit で返す。"""
        ack()
        if not authorize(command["user_id"], "user", say):
            return
        logger.info("/taka-ma-status 受信")
        blocks = build_status_blocks()
        say(blocks=blocks)

    @app.command("/taka-ma-stop")
    def handle_stop(ack, say, command):
        """/taka-ma-stop — sa-ru / ya-ta を unload する緊急停止（owner 限定）。"""
        ack()
        if not authorize(command["user_id"], "owner", say):
            return
        logger.info("/taka-ma-stop 受信 — 緊急停止")
        # sa-ru, ya-ta を unload（KeepAlive による自動再起動を防ぐ）
        results = [
            _result_line(_launchctl("unload", plist), name, "停止")
            for name, plist in _CORE_SERVICES
        ]
        say(":octagonal_sign: 緊急停止を実行しました\n" + "\n".join(results))

    @app.command("/taka-ma-start")
    def handle_start(ack, say, command):
        """/taka-ma-start — /taka-ma-stop で落とした sa-ru / ya-ta を load し直す復旧（owner 限定）。"""
        ack()
        if not authorize(command["user_id"], "owner", say):
            return
        logger.info("/taka-ma-start 受信 — サービス復旧")
        # sa-ru, ya-ta を load（unload で停止したサービスを復旧）
        results = [
            _result_line(_launchctl("load", plist), name, "起動")
            for name, plist in _CORE_SERVICES
        ]
        say(":rocket: サービスを復旧しました\n" + "\n".join(results))

    @app.command("/taka-ma-ollama-stop")
    def handle_ollama_stop(ack, say, command):
        """MBP の稼働 ollama モデルを手動 unload する（§8.10c）。

        危険操作（推論中モデルの GPU/メモリ解放）のため RBAC の owner ゲート。`/taka-ma-stop`
        の launchctl 停止とは別物で、こちらは ollama サービス自体は残し稼働モデルだけ落とす。
        停止本体は sa-ru の RemoteProcessManager.stop_ollama()（SSOT）に在り u-zu から直接呼べ
        ないため、制御ファイルを投入し sa-ru の制御ループに委譲する（経路 Slack→u-zu→sa-ru）。
        再起動は不要で、次の推論リクエストで ollama が自動再ロードする（§7.1 前提）。
        """
        ack()
        if not authorize(command["user_id"], "owner", say):
            return
        logger.info("/taka-ma-ollama-stop 受信 — 手動 ollama 停止")
        enqueue_control(
            COMMAND_STOP_OLLAMA,
            user_id=command["user_id"],
            team_id=command.get("team_id", ""),
            channel_id=command["channel_id"],
        )
        say(":hourglass_flowing_sand: MBP の ollama 停止を sa-ru に依頼しました（結果を通知します）")

    @app.command("/taka-ma-logs")
    def handle_logs(ack, say, command):
        """/taka-ma-logs — 各サービスログの末尾を Block Kit で返す（admin 限定）。"""
        ack()
        if not authorize(command["user_id"], "admin", say):
            return
        logger.info("/taka-ma-logs 受信")
        blocks = build_log_blocks()
        say(blocks=blocks)

    @app.command("/taka-ma-approve")
    def handle_approve_command(ack, say, command):
        """/taka-ma-approve <request_id> — ボタンの代替で Tier3 承認ファイルを approved に更新（§8.10、admin 限定）。"""
        ack()
        if not authorize(command["user_id"], "admin", say):
            return
        request_id = command["text"].strip()
        if not request_id:
            say(":warning: 使い方: `/taka-ma-approve <request_id>`")
            return
        logger.info("/taka-ma-approve 受信: %s", request_id)
        # ボタン押下と同じく §8.10 承認ファイルを approved に更新（sa-ru がポーリング検知）。
        if resolve_approval(request_id, "approved", user_id=command["user_id"]):
            say(f":white_check_mark: 承認しました (ID: {request_id})")
        else:
            say(f":warning: 承認できませんでした（ID 不正・処理済み・期限切れ） (ID: {request_id})")

    @app.command("/taka-ma-blender")
    def handle_blender(ack, say, command):
        """/taka-ma-blender on|off — MBP の ollama サービスを停止/再開する（admin 限定）。

        Blender 等 GPU を専有する作業のため MBP の ollama を一時退避する用途。on で停止、off で再開。
        """
        ack()
        if not authorize(command["user_id"], "admin", say):
            return
        action = command["text"].strip().lower()
        logger.info("/taka-ma-blender 受信: %s", action)
        if action == "on":
            # Blenderモード ON: MBP上のollama停止
            try:
                subprocess.run(
                    ["ssh", "mbp", "/opt/homebrew/bin/brew", "services", "stop", "ollama"],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                say(":art: Blenderモード ON — MBP の ollama を停止しました")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                say(f":x: Blenderモード切替失敗: {e}")
        elif action == "off":
            # Blenderモード OFF: MBP上のollama再開
            try:
                subprocess.run(
                    ["ssh", "mbp", "/opt/homebrew/bin/brew", "services", "start", "ollama"],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                say(":robot_face: Blenderモード OFF — MBP の ollama を再開しました")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                say(f":x: Blenderモード切替失敗: {e}")
        else:
            say(":warning: 使い方: `/taka-ma-blender on` または `/taka-ma-blender off`")

    @app.command("/taka-ma-user")
    def handle_user(ack, say, command):
        """ユーザー管理（add / update / remove / list）。

        認可方針（運用書「コマンドごとのロール要件」※注）:
        - 操作自体は admin 以上が必要。
        - owner/admin を対象にする追加・変更・削除は owner のみ（admin は user 級のみ管理可）。
        """
        ack()
        actor = command["user_id"]
        # 入口ゲート: admin 未満は一律拒否
        if not authorize(actor, "admin", say):
            return

        args = command["text"].split()
        if not args:
            say(":warning: 使い方: `/taka-ma-user add|update|remove @user [role]` / `list`")
            return
        sub = args[0].lower()

        if sub == "list":
            users = user_store.load_users()
            if not users:
                say(":information_source: 登録ユーザーはいません")
                return
            lines = [f"• `{uid}` {u.get('name', '')} — *{u.get('role', '?')}*"
                     for uid, u in sorted(users.items())]
            say(":busts_in_silhouette: 登録ユーザー\n" + "\n".join(lines))
            return

        if sub in ("add", "update", "remove"):
            if len(args) < 2:
                say(f":warning: 使い方: `/taka-ma-user {sub} @user"
                    f"{' role' if sub != 'remove' else ''}`")
                return
            target_id, target_name = _parse_user_mention(args[1])
            if not target_id:
                say(":warning: ユーザー指定が不正です（@メンションで指定してください）")
                return

            # owner/admin が絡む操作は owner 限定（admin は user 級のみ）
            new_role = args[2].lower() if len(args) >= 3 else None
            current_role = get_role(target_id)
            if not can_manage_user(get_role(actor), current_role, new_role):
                say(role_denied_message("owner") + "（owner/admin の管理は owner のみ）")
                return

            try:
                if sub == "add":
                    if not new_role:
                        say(":warning: ロールを指定してください（owner / admin / user）")
                        return
                    user_store.add_user(target_id, target_name, new_role)
                    say(f":white_check_mark: 追加: `{target_id}` を *{new_role}* で登録しました")
                elif sub == "update":
                    if not new_role:
                        say(":warning: 変更後のロールを指定してください（owner / admin / user）")
                        return
                    user_store.update_user(target_id, new_role)
                    say(f":white_check_mark: 変更: `{target_id}` を *{new_role}* に更新しました")
                else:  # remove
                    user_store.remove_user(target_id)
                    say(f":white_check_mark: 削除: `{target_id}` を登録解除しました")
            except ValueError as e:
                say(f":x: {e}")
            return

        say(":warning: 不明なサブコマンドです（add / update / remove / list）")

    @app.command("/taka-ma-model")
    def handle_model(ack, say, command):
        """モデル管理（add / update / remove / list / install / uninstall）。

        認可: admin 以上（運用書「コマンドごとのロール要件」: /taka-ma-model = Owner/Admin）。
        役割分担:
          - add / update / remove: ya-ta.yaml の models セクション編集のみ（再起動しない）。
          - install:   ya-ta.yaml の登録を反映（type:local は worker で ollama pull + 来歴記録）
                       し、sa-ru / ya-ta を再起動して設定を読み直させる。
          - uninstall: ya-ta.yaml から削除（type:local は worker で ollama rm）し、再起動。
        """
        ack()
        if not authorize(command["user_id"], "admin", say):
            return

        try:
            args = shlex.split(command["text"])
        except ValueError:
            say(":warning: 引数の引用符が閉じていません")
            return
        if not args:
            say(_model_usage())
            return
        sub = args[0].lower()

        if sub == "list":
            _model_list(say)
        elif sub in ("add", "update"):
            _model_add_or_update(sub, args[1:], say)
        elif sub == "remove":
            _model_remove(args[1:], say)
        elif sub in ("install", "uninstall"):
            _model_install_or_uninstall(sub, args[1:], say)
        else:
            say(_model_usage())
