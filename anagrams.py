
import os
import sys
from collections import Counter

import gevent
from gevent.event import AsyncResult

from ekimbot.botplugin import ClientPlugin
from ekimbot.commands import CommandHandler


words = AsyncResult()
loader = None

def load_dict():
	global words
	_words = {}
	path = '/usr/share/dict'
	for name in os.listdir(path):
		filepath = os.path.join(path, name)
		with open(filepath) as f:
			lines = f.read()
		for n, line in enumerate(lines.strip().split('\n')):
			count = tuple(sorted(Counter(line.lower()).items()))
			_words.setdefault(count, set()).add(line)
			if n % 10000 == 0:
				gevent.sleep(0.01) # let other greenlets act
	words.set(_words)


class AnagramsCommand(ClientPlugin):
	name = 'anagrams'

	def init():
		global loader
		if not (loader or words.ready()):
			loader = gevent.spawn(load_dict)

	@CommandHandler('anagrams', 1)
	def anagrams(self, msg, *args):
		word = ' '.join(args)
		matches = find_anagrams(word)
		if matches:
			self.reply(msg, "Anagrams of {}: {}".format(word, ', '.join(matches)))
		else:
			self.reply(msg, "No anagrams of {}".format(word))

	@CommandHandler('anagram', 1)
	def anagram(self, msg, *args):
		return self.anagrams(msg, *args)


def find_anagrams(word):
	count = tuple(sorted(Counter(word.lower()).items()))
	matches = filter(lambda w: w.lower() != word.lower(), words.get().get(count, set()))
	return matches


if __name__ == '__main__':
	from monotonic import monotonic
	def get_mem():
		with open('/proc/self/stat') as f:
			values = f.read().strip().split()
			rss = int(values[23]) * 4
			return rss

	test_words = sys.stdin.read().strip().split('\n')

	info = []
	info.append(("start", monotonic(), get_mem()))
	load_dict()
	info.append(("loaded", monotonic(), get_mem()))
	for word in test_words:
		find_anagrams(word)
	info.append(("look up {}x".format(len(test_words)), monotonic(), get_mem()))

	_, prev_t, prev_mem = info[0]
	FORMAT = "{}: {:+.2f}s, {:+f}MB"
	for name, t, mem in info[1:]:
		print FORMAT.format(name, t - prev_t, (mem - prev_mem) / 1024.0)
		prev_t, prev_mem = t, mem
	print "{}: {:.2f}s, {:f}MB".format("final", prev_t, prev_mem / 1024.0)
