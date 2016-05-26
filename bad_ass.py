
import re

from girc import Handler
from ekimbot.botplugin import ClientPlugin


class BadAssPlugin(ClientPlugin):
	"""Replaces "X-ass Y" with "X ass-Y"
	"""
	name = 'bad_ass'
	pattern = re.compile(r'(\w+)[-_](ass) (\w+)', re.IGNORECASE)

	@Handler(command='PRIVMSG')
	def find_ass(self, client, msg):
		for adjective, ass, noun in self.pattern.findall(msg.payload):
			self.reply(msg, "{} {}-{}?".format(adjective, ass, noun))
