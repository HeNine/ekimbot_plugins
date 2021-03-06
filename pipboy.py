
import functools
import random
import re
import time

import gpippy
import gevent.event
import gevent.lock
from mrpippy.data import Player, Inventory

from ekimbot.botplugin import ChannelPlugin
from ekimbot.commands import ChannelCommandHandler, EkimbotHandler


POLL_RESPONSE = re.compile(r'^Poll for .* has been closed. Winning option was Slot (\d+)$')


def needs_data(fn):
	@functools.wraps(fn)
	def wrapper(self, msg, *args):
		if not self.pippy:
			self.reply(msg, "Not connected.")
			return
		if self.player is None:
			self.reply(msg, "Not ready yet, please wait.")
			return
		return fn(self, msg, *args)
	return wrapper


def op_only(fn):
	@functools.wraps(fn)
	def wrapper(self, msg, *args):
		if not msg.target.startswith('#'):
			self.reply(msg, "This command is not allowed in PM")
			return
		if not self.is_op(msg):
			if self.check_cooldown('mod-only', 30):
				self.reply(msg, "This command is mod-only.")
			return
		return fn(self, msg, *args)
	wrapper.op_only = True
	return wrapper


def with_cooldown(interval):
	"""Only run the wrapped handler if it hasn't run in the last interval seconds,
	or if the caller is an op.
	"""
	def _with_cooldown(fn):
		@functools.wraps(fn)
		def wrapper(self, msg, *args):
			if self.check_cooldown(fn.__name__, interval, self.is_op(msg)):
				return fn(self, msg, *args)
		return wrapper
	return _with_cooldown


def drop_client_arg(fn):
	"""Decorator designed to adapt the call signature of a Handler to that of a
	CommandHandler, ie. it drops the redundant client arg. This is useful to allow usage of
	CommandHandler-specific decorators."""
	@functools.wraps(fn)
	def wrapper(self, client, msg, *args):
		return fn(self, msg, *args)
	return wrapper


def costs_points(points):
	"""Only allows a function to execute if a user has the points to pay for it.
	Once the function returns True, the points are deducted. The escrow system is used to avoid races.
	If the function returns False or raises, points are refunded.
	To allow calling wrapped functions without invoking a point cost, the optional kwarg free=True
	can be given.
	"""
	def _costs_points(fn):
		class WrappedFailed(Exception):
			pass
		@functools.wraps(fn)
		def wrapper(self, msg, *args, **kwargs):
			free = kwargs.pop('free', False)
			if kwargs:
				raise TypeError("Unexpected kwargs: {}".format(kwargs))
			if free:
				return fn(self, msg, *args)
			try:
				with self.deepclient.escrow(msg.sender, points):
					if not fn(self, msg, *args):
						raise WrappedFailed
			except WrappedFailed:
				pass
			except (deepclient.UserNotFound, deepclient.NotEnoughPoints):
				self.reply(msg, "{}: not enough points for that command (need {})".format(msg.sender, points))
		wrapper.point_cost = points
		return wrapper
	return _costs_points


class UseItemReset(Exception):
	pass


class UseItemLock(gevent.lock.RLock):
	"""RLock variant which blocks until we're in a state when we can use an item."""
	_use_item_waiter = None
	_last_use_version = None

	def __init__(self, parent):
		self.parent = parent
		super(UseItemLock, self).__init__()

	def acquire(self):
		super(UseItemLock, self).acquire()
		if self._count == 1:
			# on first acquire, block until we can use item
			self._use_item_waiter = gevent.event.AsyncResult()
			self.check()
			self._use_item_waiter.wait()
			if not self._use_item_waiter.successful():
				self.release()
				self._use_item_waiter.get() # raise

	def reset(self):
		if self._use_item_waiter:
			self._use_item_waiter.set_exception(UseItemReset())
		self._last_use_version = None

	def check(self):
		if (self._use_item_waiter
		    and not self._use_item_waiter.ready()
		    and self.parent.inventory.version != self._last_use_version
		    and not self.parent.player.locked):
			self._use_item_waiter.set(None)

	def set_last_use_version(self, version):
		self._last_use_version = version


class PipBoy(ChannelPlugin):
	"""Plugin for interacting with a running Fallout 4 game by means of the pip boy app protocol"""
	name = 'pipboy'

	CURRENCY_NAME = 'catnip'

	CHEMS = {
		'berry mentats',
		'buffjet',
		'buffout',
		'bufftats',
		'calmex',
		'daddy-o',
		'day tripper',
		'fury',
		'grape mentats',
		'jet',
		'jet fuel',
		'med-x',
		'mentats',
		'orange mentats',
		'overdrive',
		'psycho',
		'psycho jet',
		'psychobuff',
		'psychotats',
		'ultra jet',
		'x-cell',
	}

	# required: deep_secret - api secret for deepbot client
	defaults = {
		'host': 'localhost',
		'port': 27000,
		'deep_url': 'ws://localhost:3337',
		'force_ops': [],
	}

	pippy = None
	was_dead = None # True or False, None means unknown

	def init(self):
		self.ready = gevent.event.Event()
		self.use_item_lock = UseItemLock(self)
		self.cooldowns = {}
		self._deepclient = gevent.spawn(deepclient.DeepClient, self.config.deep_url, self.config.deep_secret)

	def cleanup(self):
		if self.pippy:
			self.pippy.close()
		super(PipBoy, self).cleanup()

	def is_op(self, msg):
		"""Returns if msg was sent by an op"""
		# on twitch, there's weirdness with 'is an op' so we hard-code that the channel owner is op
		return msg.sender in self.channel.users.ops or msg.sender == self.channel.name.lstrip('#') or msg.sender in self.config.force_ops

	def check_cooldown(self, name, interval, bypass=False):
		"""If named cooldown has not been used in the last interval seconds,
		or if bypass is True, return True and use the cooldown. Else return False.
		"""
		now = time.time()
		self.logger.debug("checking cooldown for {!r} with bypass {}".format(name, bypass))
		if not bypass and name in self.cooldowns and now - self.cooldowns[name] < interval:
			self.logger.debug("rejecting cooldown check: last used {}s ago, needed {}s".format(
				now - self.cooldowns[name],
				interval))
			return False
		self.cooldowns[name] = now
		return True

	@ChannelCommandHandler('help', 0)
	@with_cooldown(60)
	def help(self, msg):
		# find_handlers will return all attrs of self which are ChannelCommandHandlers
		lines = []
		for command in ChannelCommandHandler.find_handlers(self):
			func = command.callback
			if getattr(func, 'op_only', False):
				continue # hide op only
			points = getattr(func, 'point_cost', 0)
			cost = '({} {}) '.format(points, self.CURRENCY_NAME) if points else ''
			summary, description = command.help
			if not summary:
				summary = ''
			line = '{prefix}{name} {cost}- {help}'.format(
				prefix=self.client.config['command_prefix'],
				name=' '.join(command.name),
				cost=cost,
				help=summary,
			)
			lines.append((points, line))
		lines.sort() # sorts from free to most expensive, then alphabetically
		for points, line in lines:
			self.reply(msg, line)

	@ChannelCommandHandler('connect', 0)
	@op_only
	def connect(self, msg):
		"""Connect to the game"""
		if self.pippy:
			self.disconnect(msg)
		self.use_item_lock.reset()
		try:
			self.pippy = gpippy.Client(self.config.host, self.config.port, self.on_update, on_close=self.on_close)
		except Exception:
			self.logger.warning("Failed to connect to {config.host}:{config.port}".format(config=self.config), exc_info=True)
			self.reply(msg, "Failed to connect")
			return
		self.reply(msg, "Connected to game")

	@ChannelCommandHandler('disconnect', 0)
	@op_only
	def disconnect(self, msg):
		"""If connected, close the connection"""
		if not self.pippy:
			return
		self.pippy.close()
		assert self.pippy is None, "Failed to clear self.pippy after close returned"

	def on_update(self, update):
		player = self.player
		if not player:
			return
		# check for death
		is_dead = player.value['Status']['IsPlayerDead']
		if (self.was_dead is not None and # was_dead isn't unknown
		    is_dead and not self.was_dead and # value has gone from false to true
		    self.check_cooldown('death', 10)): # we didn't say it recently
			self.channel.msg("!death")
		self.was_dead = is_dead
		# check for waiting use_item_lock
		self.use_item_lock.check()

	def on_close(self, ex):
		self.pippy = None
		self.ready.clear()
		self.was_dead = None
		self.channel.msg("Connection lost")

	@property
	def player(self):
		if not self.pippy or self.pippy.pipdata.root is None:
			return
		return Player(self.pippy.pipdata)

	@property
	def inventory(self):
		if not self.pippy or self.pippy.pipdata.root is None:
			return
		return Inventory(self.pippy.pipdata)

	@property
	def deepclient(self):
		return self._deepclient.get()

	def use_item(self, item):
		with self.use_item_lock:
			version = self.inventory.version
			self.use_item_lock.set_last_use_version(version)
			# confirm item is still present
			found_items = [i for i in self.inventory.items if i.handle_id == item.handle_id]
			if len(found_items) > 1:
				self.logger.warning("Got duplicate handle id for multiple items: {}".format(found_items))
				found_items = found_items[:1] # take first one
			if not found_items:
				self.channel.msg("Failed to use {}: item no longer exists".format(item.name))
				return
			self.pippy.use_item(item.handle_id, version, block=False)
			self.channel.msg("Used {}".format(item.name))
			return True

	@ChannelCommandHandler('health', 0)
	@with_cooldown(60)
	@needs_data
	def health(self, msg):
		"""See player's health and other vital stats"""
		player = self.player
		limbs = {name: condition * 100 for name, condition in player.limbs.items() if condition < 1}
		limbs_str = ", ".join("{} {:.0f}%".format(name, condition) for name, condition in limbs.items())
		if not limbs_str:
			limbs_str = 'all limbs healthy'

		self.reply(msg,
			(
				"{player.name} L{level} ({level_percent}% to next), "
				"{player.hp:.0f}/{player.maxhp:.0f}hp ({hp_percent}%), {limbs}"
			).format(
				player = player,
				level = int(player.level),
				level_percent = int(100 * player.level) % 100,
				hp_percent = int(100 * player.hp / player.maxhp),
				limbs = limbs_str,
			)
		)

	@ChannelCommandHandler('info', 0)
	@with_cooldown(60)
	@needs_data
	def info(self, msg):
		"""See player's weight, location and other info"""
		player = self.player

		weight = int(player.weight)
		maxweight = int(player.maxweight)
		self.reply(msg,
			(
				"{player.name} carrying {weight}/{maxweight}lb "
				"in {player.location} at {time}"
			).format(
				player = player,
				time = time.strftime("%H:%M", time.gmtime(player.time)),
				weight = weight,
				maxweight = maxweight,
				special = ', '.join('{}: {}'.format(letter, value)
				                    for letter, value in zip('SPECIAL', player.special)),
			)
		)

	@ChannelCommandHandler('special', 0)
	@with_cooldown(60)
	@needs_data
	def special(self, msg):
		"""See player's S.P.E.C.I.A.L. and current bonuses"""
		player = self.player
		names = "STR", "PER", "END", "CHA", "INT", "AGL", "LCK"
		display = []
		for name, value, base in zip(names, player.special, player.base_special):
			diff = value - base
			suffix = '({:+d})'.format(diff) if diff else ''
			display.append("{} {}{}".format(name, value, suffix))
		self.reply(msg, "{}: {}".format(
			player.name,
			", ".join(display),
		))

	@ChannelCommandHandler('weapons', 0)
	@with_cooldown(60)
	@needs_data
	def list_weapons(self, msg):
		"""List all favorited weapon slots"""
		favorites = [item for item in self.inventory.weapons if item.favorite]
		favorites = {item.name: item for item in favorites}.values()
		favorites.sort(key=lambda item: item.favorite_slot)
		self.reply(msg, "Favorited items:")
		for item in favorites:
			slot_name = item.favorite_slot + 1
			ammo = item.ammo
			if ammo is item:
				# grenades, etc
				ammo_str = " ({}x)".format(item.count)
			elif ammo:
				# firearms
				ammo_str = " ({ammo.count}x {ammo.name})".format(ammo=ammo)
			else:
				# no ammo: melee, etc
				ammo_str = ""
			self.reply(msg, "{} - {}{}".format(slot_name, item.name, ammo_str))

	@ChannelCommandHandler('chems', 0)
	@with_cooldown(60)
	@needs_data
	def list_chems(self, msg):
		"""See a selection of chems the player is carrying"""
		LIMIT = 5
		chems = [item for item in self.inventory.aid if item.name.lower() in self.CHEMS]
		if len(chems) > LIMIT:
			chems = random.sample(chems, LIMIT)
		for item in sorted(chems, key=lambda item: item.name):
			description = ', '.join(item.effects_text)
			self.reply(msg, "{item.count}x {item.name} ({description})".format(
				item=item,
				description=description,
			))

	@ChannelCommandHandler('booze', 0)
	@needs_data
	@costs_points(25)
	def booze(self, msg):
		"""Use a random booze item"""
		with self.use_item_lock:
			inventory = self.inventory
			booze = [item for item in inventory.aid if item.name.lower() in item.ALCOHOL_NAMES]
			if not booze:
				self.reply(msg, "Sorry, {} is trying to cut back (Not carrying any booze)".format(self.player.name))
				return
			item = random.choice(booze)
			return self.use_item(item)

	@ChannelCommandHandler('usechem', 1)
	@needs_data
	@costs_points(50)
	def use_chem(self, msg, *name):
		"""Use the named chem"""
		name = ' '.join(name)
		if name.lower() not in self.CHEMS:
			self.reply(msg, "{} is not a chem we can use".format(name))
			return
		with self.use_item_lock:
			matching = [item for item in self.inventory.aid if item.name.lower() == name.lower()]
			if not matching:
				self.reply(msg, "{} is not carrying any {}".format(self.player.name, name))
				return
			if len(matching) > 1:
				self.logger.warning("Carrying multiple copies of chem named {!r}: {}".format(name, matching))
				matching = matching[0]
			item, = matching
			return self.use_item(item)

	@ChannelCommandHandler('use', 1)
	@needs_data
	@costs_points(50)
	def use(self, msg, index):
		"""Equip or use the item in the given favorite slot (1 to 12)"""
		try:
			index = int(index) - 1 # user interface is 1-indexed
		except ValueError:
			self.reply(msg, "Favorite slot must be a number, not {!r}".format(index))
			return
		with self.use_item_lock:
			inventory = self.inventory
			items = [item for item in inventory.items if item.favorite_slot == index]
			if not items:
				self.reply(msg, "No item attached to that favorite slot")
				return
			if len(items) > 1:
				first_item = items[0]
				# special case: sometimes we get duplicates with the same name? they're the same item.
				if not all(item.name == first_item.name for item in items[1:]):
					self.reply(msg, "More than one item attached to that favorite slot somehow?")
					return
				items = [first_item]
			item, = items
			if item.equipped:
				self.reply(msg, "Sorry, you can't equip something that's already equipped")
				return
			return self.use_item(item)

	@EkimbotHandler(command='PRIVMSG', payload=POLL_RESPONSE)
	@drop_client_arg
	@op_only
	def respond_to_poll(self, msg, *args):
		match = POLL_RESPONSE.match(msg.payload)
		assert match, "handler responded for non-matching message: {!r}".format(msg.payload)
		slot, = match.groups()
		slot = int(slot)
		self.use(msg, slot, free=True)
