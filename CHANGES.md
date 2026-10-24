# sakura-bot2 更新内容

## 1. ボットオーナーは権限に関係なく使える
- `utils/perms.py` を新設。管理者系コマンドはすべて `@admin_or_owner(...)`（スラッシュ）/ `@prefix_admin_or_owner(...)`（`!skr_`）に統一。
- オーナー = アプリ所有者（Teamならメンバー）＋ `.env` の `OWNER_IDS=ID,ID`（任意）。
- 対象: `/setting *` `/role` `/role_setup` `/roll` `/sync_beginner_roles` `/sync_vc_beginner_roles` `/mod *` `!skr_clear` `!skr_say`
- `/role_setup` の `default_permissions` は廃止（付けているとDiscord側でオーナーも弾かれるため）。実行時チェックに変更。
- 注意: Bot自身のロール順位より上の相手・サーバーオーナーは、オーナーでもDiscordの仕様で操作できません。

## 2. セキュリティ修正
- **`/setting *` に権限チェックが無く、誰でもサーバー設定を変更できた** → サーバー管理権限（またはオーナー）限定に。
- リマインダーで誰でも `@everyone` を鳴らせた → `@everyone/@here` は「全員にメンション」権限者のみ。
- リマインダーを他人が削除できた → 登録者・サーバー管理者・オーナーのみ。1サーバー100件まで。
- ロールパネルに @everyone / 連携ロール / Botより上位のロールを載せられないように。

## 3. バグ修正
- **冷笑削除が実際には動いていなかった**（SettingsCogに存在しないメソッドを呼んでいた）→ `/setting moderation` の設定と `bot_config.json` の旧設定の両方で動作、自動Mod ON/OFFとModログにも対応。
- **`dice.py` が `guild_settings` を別定義で共用**しており、`enabled NOT NULL` のせいで新規サーバーの `/setting` が失敗し得た → `dice_settings` に分離（既存設定は自動移行）。
- `requirements.txt` に `asyncpg` `websockets` が無かった（UTF-16だったのもUTF-8に）。`psycopg2-binary` は未使用のため削除。
- リマインダー: サーバーのタイムゾーン依存だった → 既定 `Asia/Tokyo`（`TZ_NAME` で変更可）。Bot停止中に過ぎた1回限りの通知は起動後に送信。曜日が認識できない週次登録を拒否。
- `!skr_skr_help` になっていた → `!skr_help`。
- EEW: テスト配信を無視、同一速報の続報で重複通知しない。
- `/help` が管理者コマンドを正しく分類できていなかった（グループ内）→ 修正。

## 4. 改善
- DB接続を共有プール1本に（以前は各Cogが別々に作成）。**各Cogの `cog_unload` でプールを閉じないこと。**
- 設定を30秒キャッシュ（以前は全メッセージでDB問い合わせ）。ダイスもキャッシュ＋先にパターン判定。
- ログを `logs/bot.log` にも出力（ローテーション）。エラーメッセージを原因別に表示。
- 起動時のCog読み込み順を固定。`main.py` に `if __name__ == "__main__"` を追加。

## 5. 追加した管理機能
**サーバー管理者向け（`cogs/modtools.py`）**
`/mod kick|ban|unban|timeout|untimeout|warn|warnings|delwarn|purge|slowmode|lock|unlock` ／ `/serverinfo` `/userinfo`
実行内容は `/setting moderation` のModログチャンネルに記録。

**`/setting` の追加**
`/setting features`（挨拶・自動計算のON/OFF）／ `/setting clear`（設定済みチャンネルの解除）

**ボットオーナー向け（`cogs/owner.py`）**
`!skr_cogs` `reload` `load` `unload` `sync` `stats` `dbping` `log` `shutdown` `guildinfo` `leave`
`!skr_blacklist add|remove|list`（ユーザー/サーバーのBot利用禁止。サーバーは自動退出）

## 6. 更新時の注意
- 新しいテーブル（`dice_settings` `mod_warnings` `bot_blacklist`）と列は起動時に自動作成されます。
- 起動後、スラッシュコマンドは自動同期されます（反映に時間がかかることあり）。

## 7. `/janken` `/quiz` `/timer`（残したうえで修正）
**/quiz**
- `/setting quiz` の値が実際に使われるように（NULL許容の新カラム `quiz_answer_timeout` / `quiz_total_timeout`）。未設定なら従来どおり 15秒/900秒。旧カラムの30/60は無視します。
- `/setting quiz` で専用チャンネルを設定した場合、そのチャンネルでのみ出題可能に。
- 出題者は解答不可。答えの比較は全角/半角・大文字小文字・空白を無視。問題文は1500文字まで。
**/janken**
- `janken_stats` テーブルの作成処理が無かったので追加。`DATABASE_URL2` が無ければ共有DBを使用（以前はプレースホルダのままだとCogの読み込みに失敗）。
- 2人がほぼ同時に押しても判定が1回だけになるようロック。エラーはログに出力。
- `setup()` 内の `tree.sync()`（Cog読み込みのたびに同期していた）を削除。
**/timer**
- DBに保存するため、Botを再起動しても消えません。停止中に過ぎた分は起動後に「遅れました」付きで通知。
- 1人5個まで。メッセージ（任意）を添えられます。通知はタイマーの本人だけにメンション。
