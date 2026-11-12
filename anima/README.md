# Anima プロンプトガイド（同梱）

`prompting-en.txt` は、Anima（CircleStone Labs / Comfy Org の 2B 画像生成モデル）向けの
プロンプトの書き方をまとめた英文です。3 つの出力形式（タグ＋補足文・タグのみ・自然文）の
どれでも、システムプロンプトに読み込まれます。

書いてあること:

- Anima のテキストエンコーダーがタグと文をどう読むか
- タグの順序（人数・キャラクター・作品・@作者・一般タグ）と表記
  （小文字、空白区切り、名前の一部の括弧は `\(` `\)` で逃がす）
- 品質・スコア・年代・メタ・レーティングのタグは書かないこと（貼り付け先で付けるため）
- 日本語の言い回しに対応する Danbooru タグと、タグの選び方
- 文の書き方（主語から書く、複数人物の書き分け、タグと同じ語で絵柄が引っ張られる語を避ける）
- 入力に無いものとして足してよいもの・いけないもの
- そのまま通す記法（強調構文、LoRA、BREAK、ワイルドカード）

出力形式ごとのレイアウトと手本は `prompts.py` が持ちます。

文面は下の情報源を読んで自前の言葉で書き直したもので、原文の転載ではありません。

## 情報源

- [circlestone-labs/Anima モデルカード（Hugging Face）](https://huggingface.co/circlestone-labs/Anima) — 公式。タグの順序、品質・年代・メタ・レーティングのタグ、`@作者`、自然言語の書き方
- [The Ultimate Anima Prompting Guidebook（Civitai）](https://civitai.com/articles/35368/the-ultimate-anima-prompting-guidebook-all-you-need) — タグと文を併せる構成、複数人物の書き分け
- [note: Anima のプロンプト形式の比較](https://note.com/tasty_cougar8018/n/na0979098fa50) — 4 形式の比較で、タグ＋補足文が最もよい結果だったという検証
- [Volnovik/anima-prompting（GitHub）](https://github.com/Volnovik/anima-prompting) — 小さなエンコーダー向けの平易な文、タグと同じ語になってしまう語
- [Tomiigo/anima-prompt-pipeline（GitHub）](https://github.com/Tomiigo/anima-prompt-pipeline) — 日本語から Anima プロンプトを作る流れ、括弧の逃がし

本家の更新を追う場合は上記リンク先を確認してください。
