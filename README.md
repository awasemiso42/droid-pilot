# droid-pilot（`dp`）

AI エージェント（Claude Code）が Android のエミュレータやテスト用の実機を操作して、アプリのテストとデバッグをするための小さな CLI。

- 出力は短い。1 行目は必ず `OK` / `NG` / `BLOCKED`
- UI ツリーは番号付きに縮め、スクショはパスだけ返す
- ログは自分のアプリのものだけ。クラッシュは要約する
- 操作はすべて記録し、手順ファイルにして回帰テストとして再生できる
- 外に出る操作（購入・送信・招待・公開）は、どの端末でも人の確認を通す

## 準備

```bash
ln -s ~/apps/droid-pilot/bin/dp ~/.local/bin/dp          # PATH に入れる
ln -s ~/apps/droid-pilot/claude/skills/droid-pilot ~/.claude/skills/droid-pilot   # Claude Code に使い方を教える
```

端末はテスト用の実機を USB でつなぐか、エミュレータを使う（`dp emu create/start`。`system-images;android-36;google_apis;x86_64` が要る）。

必要なもの:

- Python 3.11 以上と Pillow
- Android SDK の platform-tools・build-tools・platforms（`~/Android/Sdk`、または `ANDROID_HOME`）
- JDK（javac）。無ければ Android Studio の JDK（`~/.local/opt/android-studio/jbr`）を使う。`dp build` の Gradle にも同じものを渡す

### 端末に入れる補助（`dp setup`）

dp は端末側に小さな部品を 2 つ使う。どちらもこのリポジトリのソースから、初回に手元の PC でビルドする（Gradle なし、`~/.cache/droid-pilot` に保存）。

- `inject/`: 線と 2 本指の注入役。app_process で動き、1 本の線を 1 プロセスから送る（`adb shell input motionevent` は 1 点ごとに別プロセスで、キャンバスに線として届かないため）。`/data/local/tmp` に置くだけで、インストールはしない
- `ime/`: dp のキーボード（パッケージ `dev.droidpilot.ime`）。`dp type` の間だけこれに切り替えて文字を入れ、元のキーボードに戻す。日本語も入る。emu / test 端末だけに入れる

キーボードの初回は、**人が端末の前で** `dp setup` を実行する。MIUI は adb で新しいアプリを入れるとき端末に「USB経由でインストール」の確認を出し、数秒で「拒否」になるため。

### 生の adb を止めるフック

`claude/hooks/guard.py` は Claude Code の PreToolUse フックで、次の 2 つをする。

- `adb`・`emulator`・Gradle の `install*` を Claude が直接実行しようとしたら拒否する
- `dp … --yes`・`dp raw`・`dp register` では人に確認を出す

```jsonc
// ~/.claude/settings.json か、アプリのリポジトリの .claude/settings.json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{ "type": "command", "command": "python3 ~/apps/droid-pilot/claude/hooks/guard.py" }]
    }]
  }
}
```

### テスト用の実機

登録していない実機は `safe`（読み取りだけ）になる。何をしてもよい端末なら、人が登録する。

```bash
dp register <serial> --policy test --note "テスト用タブレット"
```

## 使い方

```
dp up                 ビルド → インストール → 起動 → 画面 → 起動後のエラー
dp screen [--mark]    番号付きの UI ツリー（--mark でスクショに番号）
dp tap 4              押す（そのあとの画面の変化を表示）
dp type "化学" --into 7   文字を入れる（日本語も）
dp draw wave --box 250,300,300,150 --stylus   ペンで線（筆圧付き）
dp zoom in            2 本指で拡大
dp log                自アプリのクラッシュとエラー
dp perf start|frames|idle|mem|apk   起動時間・フレーム・メモリ・APK の大きさ（確認なしで計測できる）
dp env dark on / font 1.3 / rotate land / reset   端末の見た目を変えて、元に戻す
dp journal            最近の操作（番号付き）
dp flow save 名前 31-38 / dp flow run 名前    手順の保存と再生
```

全部のコマンドは `dp --help`。AI 向けの説明は [claude/skills/droid-pilot/SKILL.md](claude/skills/droid-pilot/SKILL.md)。

## 設定

- `config/apps.toml`: アプリごとのパッケージ・リポジトリ・ビルドコマンド・APK の場所（`apps.example.toml` をコピーして作る。Git には入れない）
- `config/policy.toml`: 外に出る系・中で消す系の語、操作してよいパッケージ
- `config/devices.toml`: テスト用として扱う実機（`dp register` が作る。Git には入れない）
- `config/logs.toml`: `dp log` / `dp up` で除く端末のノイズ（正規表現）。除いた行数は出力に出て、`dp log --raw` で全部見られる

アプリのリポジトリには `.dp/`（スクショ・記録。中の `.gitignore` で Git から外れる）と `dpflows/`（手順。Git に入れる）ができる。

## テスト

```bash
python3 -m unittest discover -s tests
```

## ライセンス

MIT（[LICENSE](LICENSE)）
