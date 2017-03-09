
import random

from ekimbot.botplugin import ClientPlugin
from ekimbot.commands import CommandHandler

class SeagullCommand(ClientPlugin):
	name = 'seagull'

	@CommandHandler('seagull', 0)
	def seagull(self, msg, openchar='{', closechar='}', *args):
		out = []
		depth = 1
		while depth:
			if random.random() * 20 > len(out):
				out.append(openchar)
				depth += 1
			else:
				out.append(closechar)
				depth -= 1
		self.reply(msg, "".join(out))
