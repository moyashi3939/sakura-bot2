import ast
import re
import discord
from discord.ext import commands


class Calculator(commands.Cog):

  def __init__(self, bot):
    self.bot = bot

  def _eval_node(self, node):
    operators = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b,
        ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a**b,
        ast.USub: lambda a: -a,
        ast.UAdd: lambda a: +a,
    }

    if isinstance(node, ast.Constant):
      if isinstance(node.value, (int, float)):
        if abs(node.value) > 1e15:
          raise ValueError("数値が大きすぎます")
        return node.value
      raise TypeError("サポートされていない型です")

    elif isinstance(node, ast.BinOp):
      op_type = type(node.op)
      if op_type in operators:
        left_val = self._eval_node(node.left)
        right_val = self._eval_node(node.right)

        if op_type is ast.Pow:
          if isinstance(right_val, (int, float)) and right_val > 100:
            raise ValueError("指数が大きすぎます（最大100まで）")

        return operators[op_type](left_val, right_val)

    elif isinstance(node, ast.UnaryOp):
      op_type = type(node.op)
      if op_type in operators:
        return operators[op_type](self._eval_node(node.operand))

    raise TypeError("安全ではない数式、または無効な数式です")

  def safe_eval(self, expr_str: str):
    node = ast.parse(expr_str, mode="eval")
    return self._eval_node(node.body)

  @commands.Cog.listener()
  async def on_message(self, message: discord.Message):
    if message.author.bot or not message.guild:
      return

    content = message.content.strip()

    # 【制限1】長すぎる数式（40文字超）は処理しない
    if len(content) > 40:
      return

    # 【制限2】メッセージ全体が「数字、空白、演算子」だけで構成されているかチェック（日付や文章の混入を防ぐ）
    if not re.fullmatch(r"[0-9\.\s\+\-\*\/\(\)\^%]+", content):
      return

    # 演算子が含まれているかチェック
    has_operator = any(
        op in content for op in ["+", "-", "*", "/", "^", "%", "(", ")"]
    )
    if has_operator:
      try:
        clean_expr = content.replace("^", "**")
        result = self.safe_eval(clean_expr)

        if isinstance(result, float) and result.is_integer():
          result = int(result)

        embed = discord.Embed(title="🧮 計算結果", color=discord.Color.green())
        embed.add_field(
            name="数式", value=f"`{content}`", inline=False
        )
        embed.add_field(name="答え", value=f"**{result}**", inline=False)

        embed.set_footer(
            text=f"Calculated by {message.author.display_name}",
            icon_url=message.author.display_avatar.url,
        )
        await message.reply(embed=embed, mention_author=False)
      except Exception:
        pass


async def setup(bot):
  await bot.add_cog(Calculator(bot))