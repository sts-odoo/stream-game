#!/usr/bin/env python3

"""Usage:
    generate_scoreboard.py <config_file>
    generate_scoreboard.py (-h | --help)

Options:
    -h --help             Show this help message and exit
"""
import subprocess
import os
import signal
import shutil
import sys
import time
import math
import base64
from xml.sax.saxutils import escape as xml_escape
import cairosvg
from PIL import Image, ImageDraw, ImageOps
try:
    import face_recognition
    import numpy
except:
    face_recognition = None
    numpy = None
from io import BytesIO
from docopt import docopt
import configparser
from datetime import datetime
import gevent
from gevent import monkey

import logging

monkey.patch_all()
import requests

ARGS = docopt(__doc__)

logger = logging.getLogger(__name__)

config = configparser.ConfigParser()
config.read(ARGS['<config_file>'])

WEBSITE_URL = config.get('baseball', 'website_url')
WORKING_DIR = config.get('baseball', 'working_dir')
TIMEOUT = 30
# watchdog: restart ffmpeg if it makes no output progress for that many seconds
STREAM_STALL_TIMEOUT = 30
# watchdog: leave ffmpeg time to connect to the RTMP server after a (re)start
STREAM_STARTUP_GRACE = 60
HOME_NAME = 'home'
AWAY_NAME = 'away'
BASE_URL = 'https://game.wbsc.org/gamedata'
LATEST_PLAY_URL = '%s/%%s/latest.json' % (BASE_URL)
PLAY_URL = '%s/%%s/play%%s.json' % (BASE_URL)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.97 Safari/537.36"}
FIELD_IMAGE = 'https://static.wbsc.org/public/wbsc/images/baseball-field.svg'
DEFAULT_IMAGE_URL = 'https://static.wbsc.org/assets/images/default-player.jpg'
# STATS https://www.wbsc.org/api/v1/player/stats?tab=charts&fedId=143&eventId=2115&roundId=all&gameId=all&pId=649920&teamId=29254
INPUT_CAMERA_STREAM_FIELD1 = config.has_option('baseball', 'input_stream_1') and config.get('baseball', 'input_stream_1')
INPUT_CAMERA_STREAM_FIELD2 = config.has_option('baseball', 'input_stream_2') and config.get('baseball', 'input_stream_2')
FINE_TUNE_CAMERA_FIELD1 = 'rotate=0.06,crop=2100:980:100:100,'

FINE_TUNE_CAMERA_FIELD2 = ''

MAIN_STREAM = config.get('baseball', 'main_rtmp_stream')
BACKUP_STREAM = config.has_option('baseball', 'backup_rtmp_stream') and config.get('baseball', 'backup_rtmp_stream')

LOGFILE = config.has_option('baseball', 'logfile') and config.get('baseball', 'logfile')
INPUT_RESOLUTION = (1920, 1080)

PHOTO_WIDTH = 470

# Scorebug layout (see Game.get_scorebug): a two-row left column (team logo +
# that team's own color as the score cell background, with the text color
# picked automatically for contrast), a shared dark panel on the right
# (inning, bases diamond, outs, count), and a pitcher-name strip below.
#
# make_overlay() resizes the rasterized card to resolution[0] /
# SCOREBUG_DISPLAY_SCALE wide on the final video frame (e.g. ~384px on a
# 1920-wide stream) -- that happens after rasterization, so it doesn't affect
# any of the geometry below.
SCOREBUG_LOGO_COL_W = 90
SCOREBUG_SCORE_COL_W = 170
SCOREBUG_RIGHT_W = 250
SCOREBUG_ROW_H = 85
SCOREBUG_PITCHER_ROW_H = 54
SCOREBUG_RADIUS = 16
SCOREBUG_ACCENT = (22, 35, 63)  # neutral navy for the shared bases/inning panel -- not either team's color
SCOREBUG_DIAMOND_RADIUS = 16
SCOREBUG_DIAMOND_GAP = 8  # gap between the 3 base markers, beyond just touching
SCOREBUG_BASE_MARKER_RADIUS = 7  # also used for the outs dots
SCOREBUG_DISPLAY_SCALE = 5
CARD_BORDER_COLOR = '#e2e2e5'  # the one light-gray border/divider color used on every card
FONT_FAMILY = 'Nimbus Sans'
FONT_SIZE_SCOREBUG_CODE = 28
FONT_SIZE_SCOREBUG_SCORE = 46
FONT_SIZE_SCOREBUG_INNING = 36
FONT_SIZE_SCOREBUG_COUNT = 36
FONT_SIZE_SCOREBUG_PITCHER = 24
FONT_SIZE_SCOREBUG_PITCH_COUNT = 20


def _svg_hex(rgb):
    return '#%02x%02x%02x' % tuple(rgb[:3])


def _svg_diamond_points(cx, cy, r):
    """SVG polygon `points` string for a diamond (square rotated 45°) of
    half-diagonal `r`, centered on (cx, cy)."""
    return '%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f' % (cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy)


def _image_data_uri(image):
    buf = BytesIO()
    image.convert('RGBA').save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def _svg_logo_tag(image, box_x, box_y, box_w, box_h, pad=0.14):
    """<image> tag that fits `image` *inside* the given box (letterboxed, not
    cropped) -- team logos are rarely perfectly square. Returns '' if there's
    no logo, rather than erroring."""
    if not image:
        return ''
    nat_w, nat_h = image.size
    avail_w, avail_h = box_w * (1 - pad), box_h * (1 - pad)
    scale = min(avail_w / nat_w, avail_h / nat_h)
    w, h = nat_w * scale, nat_h * scale
    x = box_x + (box_w - w) / 2
    y = box_y + (box_h - h) / 2
    return '<image href="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>' % (_image_data_uri(image), x, y, w, h)


def _svg_text(x, y, text, size, fill, anchor='start', opacity=None, family=FONT_FAMILY, bold=True, max_width=None):
    """<text> element, always escaped -- routing every scorebug/batter text
    element through here means a dynamic value (team code, pitcher surname)
    can't end up unescaped in the markup just because a future edit forgot
    the usual xml_escape() call.

    `max_width`, if given, sets textLength/lengthAdjust so the renderer
    itself compresses the glyphs to fit -- a guaranteed exact width, unlike
    estimating a font size from character count."""
    weight = 'bold' if bold else 'normal'
    attrs = 'font-family="%s" font-weight="%s" font-size="%d" fill="%s"' % (family, weight, size, fill)
    if opacity is not None:
        attrs += ' fill-opacity="%s"' % opacity
    if anchor != 'start':
        attrs += ' text-anchor="%s"' % anchor
    if max_width is not None:
        attrs += ' textLength="%s" lengthAdjust="spacingAndGlyphs"' % max_width
    return '<text x="%s" y="%s" %s>%s</text>' % (x, y, attrs, xml_escape(str(text)))


def _svg_rect(x, y, w, h, fill, rx=None):
    rx_attr = f' rx="{rx}"' if rx is not None else ''
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"{rx_attr}/>'


def _svg_line(x1, y1, x2, y2, stroke=CARD_BORDER_COLOR, width=3):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{width}"/>'


def _svg_slanted_bar_points(left, right, top, bottom, slant):
    """SVG polygon `points` for the same parallelogram shape as the PIL
    _slanted_bar() helper: spans [left, right] x [top, bottom], right edge
    shifted left by `slant` at the bottom."""
    return f'{left},{top} {right},{top} {right - slant},{bottom} {left},{bottom}'


def _svg_pie_slice(cx, cy, r, start_deg, end_deg):
    """SVG path for a pie wedge from start_deg to end_deg, using PIL's
    pieslice() angle convention (0=east/+x, clockwise in image coordinates,
    i.e. the same direction as SVG's sweep-flag=1 in a y-down system)."""
    start_rad, end_rad = math.radians(start_deg), math.radians(end_deg)
    x1, y1 = cx + r * math.cos(start_rad), cy + r * math.sin(start_rad)
    x2, y2 = cx + r * math.cos(end_rad), cy + r * math.sin(end_rad)
    large_arc = 1 if (end_deg - start_deg) % 360 > 180 else 0
    return f'M {cx},{cy} L {x1:.1f},{y1:.1f} A {r},{r} 0 {large_arc},1 {x2:.1f},{y2:.1f} Z'


def _svg_arc(cx, cy, r, start_deg, end_deg):
    """Like _svg_pie_slice, but just the curved edge itself (no radii lines
    to the center, not closed) -- for stroking an arc, e.g. a border running
    around the outside of a pie-slice badge."""
    start_rad, end_rad = math.radians(start_deg), math.radians(end_deg)
    x1, y1 = cx + r * math.cos(start_rad), cy + r * math.sin(start_rad)
    x2, y2 = cx + r * math.cos(end_rad), cy + r * math.sin(end_rad)
    large_arc = 1 if (end_deg - start_deg) % 360 > 180 else 0
    return f'M {x1:.1f},{y1:.1f} A {r},{r} 0 {large_arc},1 {x2:.1f},{y2:.1f}'


def _svg_corner_cut(corner, towards_pt, r):
    """Point `r` units from `corner`, along the segment toward `towards_pt`
    (clamped to at most half that segment). Used to cut back a polygon edge
    before rounding the corner with a quadratic curve through the original
    vertex."""
    cx, cy = corner
    dx, dy = towards_pt[0] - cx, towards_pt[1] - cy
    length = math.hypot(dx, dy) or 1
    r = min(r, length / 2)
    return (cx + dx / length * r, cy + dy / length * r)


def _svg_rounded_quad_path(p1, p2, p3, p4, r1=0, r2=0, r3=0, r4=0):
    """Closed SVG path for the quadrilateral p1->p2->p3->p4 (a rectangle or,
    for the slanted bars/rows, a parallelogram), rounding each corner by its
    own radius (0 = sharp) -- so just the corners that double as the card's
    outer silhouette can be softened, like the scorebug's card border,
    without touching the ones shared with an internal divider or hidden
    behind the photo."""
    points = [p1, p2, p3, p4]
    radii = [r1, r2, r3, r4]
    d = []
    for i in range(4):
        prev_pt, corner, next_pt = points[i - 1], points[i], points[(i + 1) % 4]
        r = radii[i]
        start = _svg_corner_cut(corner, prev_pt, r) if r else corner
        end = _svg_corner_cut(corner, next_pt, r) if r else corner
        d.append(('M %.1f,%.1f' if i == 0 else 'L %.1f,%.1f') % start)
        if r:
            d.append('Q %.1f,%.1f %.1f,%.1f' % (corner[0], corner[1], end[0], end[1]))
    d.append('Z')
    return ' '.join(d)


# Current-batter card layout (see Game.get_current_batter): two slanted bars
# plus a circular photo/placeholder badge.
BATTER_CARD_SIZE = (2500, 500)
BATTER_PORTRAIT_SIZE = 500  # diameter of the circular photo area, left-aligned
BATTER_PORTRAIT_PADDING = 15
BATTER_PORTRAIT_PLACEHOLDER_RADIUS = 150  # pie-slice badge shown when there is no photo
BATTER_BAR_LEFT = BATTER_PORTRAIT_SIZE // 2  # the two bars start at the portrait's center
BATTER_BAR_HEIGHT = 150
BATTER_BAR_SLANT = 100
BATTER_NAME_BAR_TOP = 100
BATTER_TEXT_LEFT = 550
BATTER_TEXT_PADDING_TOP = 20
BATTER_CORNER_RADIUS = 50  # softens the card's two true outer corners, like SCOREBUG_RADIUS
FONT_SIZE_BATTER_NAME = 80
FONT_SIZE_BATTER_STAT = 60

# Lineup card layout (see Game.get_lineup).
LINEUP_CARD_WIDTH = 500
LINEUP_HEADER_HEIGHT = 100
LINEUP_ROW_HEIGHT = 50
LINEUP_ROW_SLANT = 50
LINEUP_PADDING = 10
LINEUP_MAX_ROWS = 10  # 9 lineup spots + 1 pitcher
LINEUP_BATTING_ORDER_X = 10
LINEUP_NAME_X = 50
LINEUP_NAME_MAX_WIDTH = 350
LINEUP_POSITION_X = 400
LINEUP_CORNER_RADIUS = SCOREBUG_RADIUS  # this card is a similar size -- an actual shared value, not a coincidence
FONT_SIZE_LINEUP_HEADER = 60
FONT_SIZE_LINEUP_LABEL = 30


def hex2rgb(hex_color):
    if not hex_color:
        return False
    hex_color = hex_color.lstrip('#')
    rgb = tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return rgb


def get_text_color(background_color):
    brightness = (0.299 * background_color[0] + 0.587 * background_color[1] + 0.114 * background_color[2]) / 255
    if brightness > 0.5:
        return (0, 0, 0)
    else:
        return (255, 255, 255)


class Player:
    def __init__(self, game, team, player_data, lineupcode):
        self.team = team
        self.game = game
        self.id = player_data.get('playerid')
        self.team_id = player_data.get('teamid')
        self.name = player_data.get('name')
        self.firstname = player_data.get('firstname')
        self.lastname = player_data.get('lastname')
        self.image_url = player_data.get('image')
        if self.image_url == DEFAULT_IMAGE_URL:
            self.image = None
        else:
            self.image = Image.open(BytesIO(self.game.session.get(self.image_url, timeout=TIMEOUT).content))
            self.create_circle_mask()
        self.stats = player_data.get('SEASON')
        self.update(player_data, lineupcode)

    def create_circle_mask(self):
        width, height = self.image.size
        self.image = self.image.resize((PHOTO_WIDTH, int(PHOTO_WIDTH * height / width)))
        if self.image.mode != "RGB":
            image = self.image.convert("RGB")
        else:
            image = self.image
        face_locations = None
        if face_recognition:
            image_np = numpy.array(image)
            face_locations = face_recognition.face_locations(image_np)

        width, height = self.image.size
        if face_locations:
            mask = Image.new("L", self.image.size, 0)
            top, right, bottom, left = face_locations[0]
            face_center_x = (left + right) // 2
            face_center_y = (top + bottom) // 2
            radius = min(face_center_x, width - face_center_x, face_center_y, height - face_center_y)
            ellipse = ( face_center_x - radius, face_center_y - radius,
                        face_center_x + radius, face_center_y + radius)
            left = face_center_x - radius
            upper = face_center_y - radius
            right = face_center_x + radius
            lower = face_center_y + radius
        else:
            logger.info('no face found for %s', self.name)
            size = (PHOTO_WIDTH, PHOTO_WIDTH)
            mask = Image.new('L', size, 0)
            ellipse = (0, 0) + size
        draw = ImageDraw.Draw(mask)
        draw.ellipse(ellipse, fill=255)
        self.image = ImageOps.fit(self.image, mask.size, centering=(0.5, 0.5))
        self.image.putalpha(mask)
        if face_locations:
            self.image = self.image.crop((left, upper, right, lower))
            self.image = self.image.resize((PHOTO_WIDTH, PHOTO_WIDTH))

    def update(self, data, lineupcode):
        self.batting_order = lineupcode[2]
        self.position = data.get('POS')
        if not self.position and data.get('PITCHIP'):
            self.position = 'P'
        self.pa = data.get('PA')
        self.ab = data.get('AB')
        self.r = data.get('R')
        self.h = data.get('H')
        self.rbi = data.get('RBI')
        self.bb = data.get('BB')
        self.so = data.get('SO')
        self.double = data.get('DOUBLE')
        self.triple = data.get('TRIPLE')
        self.hr = data.get('HR')
        self.sf = data.get('SF')
        self.hbp = data.get('HBP')
        self.sb = data.get('SB')
        self.cs = data.get('CS')
        self.pitches = data.get('PITCHES', 0)
        self.strikes = data.get('STRIKES', 0)
        self.balls = data.get('BALLS', 0)


class Team:
    def __init__(self, game, id, code, players, logo_url, primary_color, secondary_color):
        self.game = game
        self.id = id
        self.code = code
        self.lineup = {
            players[lineupcode]['playerid']: Player(self.game, self, players[lineupcode], lineupcode)
            for lineupcode in players
            if players[lineupcode]['teamid'] == id and lineupcode[2] != '0'
        }
        self.all_players = {playerid: player for playerid, player in self.lineup.items()}
        pitcher_candidates = [(p, lineupcode)
            for lineupcode, p in players.items()
            if (p.get('POS') == 'P' or p.get('PITCHIP')) and players[lineupcode]['teamid'] == id]
        if pitcher_candidates:
            player, lineupcode = pitcher_candidates[0]
            self.pitcher = self.get_player(player, lineupcode)
            self.all_players[self.pitcher.id] = self.pitcher
        else:
            logger.warning('No pitcher found in boxscore for team %s', id)
            self.pitcher = None
        self.primary_color = primary_color
        self.secondary_color = secondary_color
        self.image = False
        if logo_url:
            self.image = Image.open(BytesIO(self.game.session.get(logo_url, timeout=TIMEOUT).content))

    def update(self, data):
        for lineupcode, player in data.items():
            if player.get('teamid') != self.id:
                continue
            if lineupcode[2] != '0':
                if player.get('playerid') in self.lineup:
                    self.lineup[player['playerid']].update(player, lineupcode)
                else:
                    self.lineup[player['playerid']] = self.get_player(player, lineupcode)
            elif player.get('POS') == 'P' or player.get('PITCHIP'):
                # TODO: multiple pitchers?
                if player.get('playerid') == self.pitcher.id:
                    self.pitcher.update(player, lineupcode)
                else:
                    self.pitcher = self.get_player(player, lineupcode)

    def get_player(self, player, lineupcode):
        if self.all_players.get(player['playerid']):
            self.all_players[player['playerid']].update(player, lineupcode)
            return self.all_players[player['playerid']]
        else:
            new_player = Player(self.game, self, player, lineupcode)
            self.all_players[new_player.id] = new_player
            return new_player

    def get_lineup(self):
        lineup = sorted([player for x, player in self.lineup.items()], key= lambda p: p.batting_order)
        if self.pitcher and self.pitcher not in lineup:
            lineup.append(self.pitcher)
        return lineup


class Game:
    def __init__(self, game_info, mode='live', replay_mode='realtime', resolution=INPUT_RESOLUTION):
        gameid = game_info.get('live_score_id')
        self.id = gameid
        self.resolution = resolution
        self.mode = mode
        self.replay_mode = replay_mode
        self.stream_proc = None
        self.game_started = False
        self.force_end = False
        self.stream_restart_count = None
        self.progress_file = os.path.join(WORKING_DIR, 'ffmpeg-progress.log')
        self.last_progress = -1
        self.last_progress_time = time.time()
        self.stream_started_at = time.time()
        self.game_info = game_info
        self.logfile = open(LOGFILE, 'a') if LOGFILE else None
        self.initialize_stream()
        self.init_game()

    def init_game(self):
        try:
            self.session = requests.Session()
            if self.mode == 'live':
                last_play = self.session.get(LATEST_PLAY_URL % self.id, headers=HEADERS, timeout=TIMEOUT)
                last_play = int(last_play.json())
                self.current_play = last_play
                data = self.session.get(PLAY_URL % (self.id, last_play), headers=HEADERS, timeout=TIMEOUT)
                data = data.json()
            else:
                self.current_play = 1
                data = self.session.get(PLAY_URL % (self.id, self.current_play), headers=HEADERS, timeout=TIMEOUT)
                data = data.json()
            self.beginning = int(data.get('playdata')[0].get('t'))
            self.data = data
            home_id = data.get('eventhomeid')
            away_id = data.get('eventawayid')
            players = data.get('boxscore')
            self.home = Team(self, home_id, data.get('eventhome'), players, self.game_info.get('home_logo'), hex2rgb(self.game_info.get('home_primary_color')), hex2rgb(self.game_info.get('home_secondary_color')))
            self.away = Team(self, away_id, data.get('eventaway'), players, self.game_info.get('away_logo'), hex2rgb(self.game_info.get('away_primary_color')), hex2rgb(self.game_info.get('away_secondary_color')))
            self.update_game(data)
            self.game_started = True
        except Exception:
            logger.exception('Could not initialize game from wbsc')

    def update_game(self, data):
        self.data = data
        pitcherid = data.get('situation')['pitcherid']
        batterid = data.get('situation')['batterid']

        self.home.update(data.get('boxscore'))
        self.away.update(data.get('boxscore'))

        self.batter = self.home.lineup.get(batterid) or self.away.lineup.get(batterid)
        self.pitcher = self.home.pitcher if self.home.pitcher and self.home.pitcher.id == pitcherid else self.away.pitcher
        self.score_home = data.get('linescore').get('hometotals').get('R')
        self.score_away = data.get('linescore').get('awaytotals').get('R')
        self.inning = data.get('situation')['currentinning'].split()[-1]
        if self.inning == 'FINAL':
            self.inning = 'F'
        self.inning_top = data.get('situation')['currentinning'].split()[0] == 'TOP'
        self.play_time = int(data.get('playdata')[0].get('t'))
        self.runner1 = data.get('situation').get('runner1')
        self.runner2 = data.get('situation').get('runner2')
        self.runner3 = data.get('situation').get('runner3')
        self.outs = data.get('situation').get('outs')
        self.balls = data.get('situation').get('balls')
        self.strikes = data.get('situation').get('strikes')

    def get_current_batter(self):
        """Two slanted bars plus a circular photo/placeholder badge, floating
        directly on the transparent overlay (no card background of its own)."""
        team_primary = self.batter.team.primary_color or SCOREBUG_ACCENT
        team_secondary = self.batter.team.secondary_color or SCOREBUG_ACCENT
        main_color = _svg_hex(team_primary)
        second_color = 'white'
        third_color = _svg_hex(team_secondary)
        text_main_color = _svg_hex(get_text_color(team_primary))

        # What the stat line says.
        if self.batter.pa:
            player_label = 'This game: %s for %s' % (self.batter.h, self.batter.ab)
            if self.batter.hr:
                player_label += ', %s %s' % (self.batter.hr, 'HR')
            elif self.batter.triple:
                player_label += ', %s %s' % (self.batter.triple, 'triple')
            elif self.batter.double:
                player_label += ', %s %s' % (self.batter.double, 'double')
            if self.batter.bb:
                player_label += ', %s %s' % (self.batter.bb, 'BB')
        else:
            average = (1.00 * int(self.batter.stats.get('H', '0')) / int(self.batter.stats.get('AB', '0'))
                                                     if int(self.batter.stats.get('AB', '0')) else 0.00)
            average = '%.3f' % average
            if average.startswith('0'):
                average = average[1:]
            player_label = 'This season: %s avg' % average
            for stat, label in [('H', 'H'), ('DOUBLE', '2B'), ('TRIPLE', '3B'), ('HR', 'HR'), ('BB', 'BB')]:
                if self.batter.stats.get(stat) != '0':
                    player_label += ', %s %s' % (self.batter.stats.get(stat), label)
        if config.has_option('baseball', 'test_time') and config.get('baseball', 'test_time'):
            player_label = str(datetime.now())

        width, height = BATTER_CARD_SIZE
        name_bar_top = BATTER_NAME_BAR_TOP
        name_bar_bottom = name_bar_top + BATTER_BAR_HEIGHT
        stat_bar_bottom = name_bar_bottom + BATTER_BAR_HEIGHT

        # The two bars' four corners, named the same way on both: left edge
        # at the photo (always hidden, so left sharp), a shared middle
        # vertex at the divider seam (also left sharp, like the scorebug's
        # own internal cell edges), and the one genuinely "outer" corner of
        # each bar rounded -- same treatment as the scorebug's card border,
        # which only softens the whole card's outline, not every internal
        # edge.
        name_tl, name_tr = (BATTER_BAR_LEFT, name_bar_top), (width, name_bar_top)
        name_br, name_bl = (width - BATTER_BAR_SLANT, name_bar_bottom), (BATTER_BAR_LEFT, name_bar_bottom)
        stat_tl, stat_tr = (BATTER_BAR_LEFT, name_bar_bottom), (width - BATTER_BAR_SLANT, name_bar_bottom)
        stat_br, stat_bl = (width - 2 * BATTER_BAR_SLANT, stat_bar_bottom), (BATTER_BAR_LEFT, stat_bar_bottom)
        R = BATTER_CORNER_RADIUS

        parts = []
        add = parts.append
        add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">')
        add(f'<path d="{_svg_rounded_quad_path(name_tl, name_tr, name_br, name_bl, r2=R)}" fill="{main_color}"/>')
        add(f'<path d="{_svg_rounded_quad_path(stat_tl, stat_tr, stat_br, stat_bl, r3=R)}" fill="{second_color}"/>')

        # Divider between the two bars, same style as the scorebug's row
        # dividers -- the bars' own edges already meet exactly at this seam
        # (bar 1's bottom edge and bar 2's top edge both span x=250..2400 at
        # y=250), so this just draws a visible line along it.
        add(_svg_line(BATTER_BAR_LEFT, name_bar_bottom, width - BATTER_BAR_SLANT, name_bar_bottom, width=6))

        # Outline around the bars, same style as the scorebug's card border --
        # left open on the side facing the photo (no segment at
        # x=BATTER_BAR_LEFT), since the circular photo already has its own
        # clear edge there. Rounds the same two corners as the bars' fill
        # above, so the stroke traces their edge exactly. Drawn before the
        # photo/placeholder badge below so that badge -- whose circle extends
        # past x=BATTER_BAR_LEFT -- stays on top of it rather than the
        # border cutting across the image or the ring.
        tr_in, tr_out = _svg_corner_cut(name_tr, name_tl, R), _svg_corner_cut(name_tr, name_br, R)
        br_in, br_out = _svg_corner_cut(stat_br, stat_tr, R), _svg_corner_cut(stat_br, stat_bl, R)
        border_path = (f'M {name_tl[0]},{name_tl[1]} '
                        f'L {tr_in[0]:.1f},{tr_in[1]:.1f} Q {name_tr[0]},{name_tr[1]} {tr_out[0]:.1f},{tr_out[1]:.1f} '
                        f'L {br_in[0]:.1f},{br_in[1]:.1f} Q {stat_br[0]},{stat_br[1]} {br_out[0]:.1f},{br_out[1]:.1f} '
                        f'L {stat_bl[0]},{stat_bl[1]}')
        add(f'<path d="{border_path}" fill="none" stroke="{CARD_BORDER_COLOR}" stroke-width="6"/>')

        portrait_center = BATTER_PORTRAIT_SIZE // 2
        if self.batter.image:
            # No extra outline here: the photo's own circular edge already
            # reads as a clear boundary against the backdrop.
            add(f'<circle cx="{portrait_center}" cy="{portrait_center}" r="{portrait_center}" fill="{third_color}"/>')
        else:
            r = BATTER_PORTRAIT_PLACEHOLDER_RADIUS
            add(f'<path d="{_svg_pie_slice(portrait_center, portrait_center, r, 180, 270)}" fill="{main_color}"/>')
            add(f'<path d="{_svg_pie_slice(portrait_center, portrait_center, r, 90, 180)}" fill="{second_color}"/>')
            # Edge for the badge's curved (left) side, matching the card
            # border's style -- the photo gets this for free from its own
            # circle/ring, but the badge has no edge of its own. Its flat
            # side (the diameter, at x=BATTER_BAR_LEFT) needs no line: it
            # meets the bars there, in matching colors.
            add(f'<path d="{_svg_arc(portrait_center, portrait_center, r, 90, 270)}" fill="none" stroke="{CARD_BORDER_COLOR}" stroke-width="6"/>')
            # The badge is split into the same two colors at the same seam
            # as the bars, so the divider continues across it too, all the
            # way to the badge's own left edge.
            add(_svg_line(portrait_center - r, name_bar_bottom, BATTER_BAR_LEFT, name_bar_bottom, width=6))

        # Vertically centered in each bar: baseline sits `0.35 * font-size`
        # below the bar's own vertical midpoint, the usual approximation for
        # a single line of text (accounts for the gap between a font's
        # baseline and its true visual center).
        name_line = f'{self.batter.batting_order}. {self.batter.name} - {self.batter.position}'
        name_bar_mid = (name_bar_top + name_bar_bottom) / 2
        stat_bar_mid = (name_bar_bottom + stat_bar_bottom) / 2
        add(_svg_text(BATTER_TEXT_LEFT, name_bar_mid + FONT_SIZE_BATTER_NAME * 0.35,
                       name_line, FONT_SIZE_BATTER_NAME, text_main_color))
        add(_svg_text(BATTER_TEXT_LEFT, stat_bar_mid + FONT_SIZE_BATTER_STAT * 0.35,
                       player_label, FONT_SIZE_BATTER_STAT, 'black'))

        if self.batter.image:
            w, h = self.batter.image.size
            add(f'<image href="{_image_data_uri(self.batter.image)}" x="{BATTER_PORTRAIT_PADDING}" y="{BATTER_PORTRAIT_PADDING}" width="{w}" height="{h}"/>')

        add('</svg>')

        png_bytes = cairosvg.svg2png(bytestring=''.join(parts).encode())
        return Image.open(BytesIO(png_bytes)).convert('RGBA')

    def get_lineup(self, team, filename):
        """Header (team logo + name) above the roster: one bordered, rounded
        row per player, separated by the gap between them rather than a
        divider line."""
        team_primary = team.primary_color or SCOREBUG_ACCENT
        team_secondary = team.secondary_color or SCOREBUG_ACCENT
        bg_hex = _svg_hex(team_primary)
        pitcher_hex = _svg_hex(team_secondary)
        text_main_color = _svg_hex(get_text_color(team_primary))
        text_pitcher_color = _svg_hex(get_text_color(team_secondary))
        row_opacity = 220 / 255  # semi-transparent so the rows read as a color-coded overlay, not a solid block

        width = LINEUP_CARD_WIDTH
        height = LINEUP_HEADER_HEIGHT + (LINEUP_ROW_HEIGHT + LINEUP_PADDING) * LINEUP_MAX_ROWS

        parts = []
        add = parts.append
        add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">')

        add(f'<rect x="{LINEUP_PADDING}" y="{LINEUP_PADDING}" width="{width - 2 * LINEUP_PADDING}" '
            f'height="{LINEUP_HEADER_HEIGHT - 2 * LINEUP_PADDING}" fill="{bg_hex}" fill-opacity="{row_opacity}" '
            f'rx="{LINEUP_CORNER_RADIUS}"/>')
        add(_svg_logo_tag(team.image, int(LINEUP_PADDING * 1.5), int(LINEUP_PADDING * 1.5),
                           LINEUP_HEADER_HEIGHT - 3 * LINEUP_PADDING, LINEUP_HEADER_HEIGHT - 3 * LINEUP_PADDING))
        header_baseline = LINEUP_HEADER_HEIGHT / 2 + FONT_SIZE_LINEUP_HEADER * 0.35
        add(_svg_text(150, header_baseline, filename.upper(), FONT_SIZE_LINEUP_HEADER, text_main_color))

        position = LINEUP_HEADER_HEIGHT
        for player in team.get_lineup():
            is_pitcher = player.batting_order == '0'
            row_fill = pitcher_hex if is_pitcher else bg_hex
            row_text = text_pitcher_color if is_pitcher else text_main_color
            row_baseline = position + LINEUP_ROW_HEIGHT / 2 + FONT_SIZE_LINEUP_LABEL * 0.35

            # Each row is its own rounded chip (all 4 corners, not just the
            # roster block's outer ones) -- the slant otherwise leaves every
            # row with a sharp, pointy corner on the right -- outlined with
            # the same border style used elsewhere on the card.
            row_top, row_bottom = position, position + LINEUP_ROW_HEIGHT
            row_tl, row_tr = (0, row_top), (width, row_top)
            row_br, row_bl = (width - LINEUP_ROW_SLANT, row_bottom), (0, row_bottom)
            row_path = _svg_rounded_quad_path(row_tl, row_tr, row_br, row_bl,
                                               r1=LINEUP_CORNER_RADIUS, r2=LINEUP_CORNER_RADIUS,
                                               r3=LINEUP_CORNER_RADIUS, r4=LINEUP_CORNER_RADIUS)
            add(f'<path d="{row_path}" fill="{row_fill}" fill-opacity="{row_opacity}"/>')
            add(f'<path d="{row_path}" fill="none" stroke="{CARD_BORDER_COLOR}" stroke-width="2"/>')
            if not is_pitcher:
                add(_svg_text(LINEUP_BATTING_ORDER_X, row_baseline, player.batting_order, FONT_SIZE_LINEUP_LABEL, row_text))

            # A long name still needs to fit before LINEUP_NAME_MAX_WIDTH
            # instead of overflowing into the position column. SVG text
            # isn't measured before rendering, so there's no way to know its
            # true width up front -- this character-count estimate is kept
            # deliberately conservative (erring toward "compress it") and
            # only *decides whether* to compress; the actual fit is then
            # guaranteed exactly by textLength/lengthAdjust below, not by
            # the estimate itself.
            player_name = '%s %s.' % (player.lastname.upper(), player.firstname[0])
            avg_char_width = FONT_SIZE_LINEUP_LABEL * 0.62
            name_max_width = LINEUP_NAME_MAX_WIDTH if len(player_name) * avg_char_width > LINEUP_NAME_MAX_WIDTH else None
            add(_svg_text(LINEUP_NAME_X, row_baseline, player_name, FONT_SIZE_LINEUP_LABEL, row_text, max_width=name_max_width))
            add(_svg_text(LINEUP_POSITION_X, row_baseline, player.position, FONT_SIZE_LINEUP_LABEL, row_text))
            position += LINEUP_PADDING + LINEUP_ROW_HEIGHT

        add('</svg>')
        png_bytes = cairosvg.svg2png(bytestring=''.join(parts).encode())
        return Image.open(BytesIO(png_bytes)).convert('RGBA')

    def get_scorebug(self):
        """Build the scorebug as an SVG string from the game's current state
        and rasterize it with cairosvg. Content is computed fresh on every
        call -- make_overlay() just calls this again each time it needs an
        up-to-date scorebug."""
        home_score = str(self.score_home)
        away_score = str(self.score_away)
        pitcher_lastname = self.pitcher.lastname if self.pitcher else ''
        pitcher_pitches = self.pitcher.pitches if self.pitcher else 0

        left_w = SCOREBUG_LOGO_COL_W + SCOREBUG_SCORE_COL_W
        width = left_w + SCOREBUG_RIGHT_W
        top_h = SCOREBUG_ROW_H * 2
        height = top_h + SCOREBUG_PITCHER_ROW_H

        # Truncate the pitcher's surname with an ellipsis if it wouldn't fit
        # before the pitch count on the right -- without this, a long name
        # either overlaps "P:57" or gets hard-clipped by the rounded corner
        # mask mid-letter. This is a rough character-count estimate (SVG text
        # isn't measured before rendering), tuned generously to avoid
        # clipping rather than to pack the text as tightly as possible.
        name_max_right = width - 110
        max_chars = max(int((name_max_right - 16) / 15), 3)
        if len(pitcher_lastname) > max_chars:
            pitcher_lastname = pitcher_lastname[:max_chars - 1].rstrip() + '…'

        parts = []
        add = parts.append
        add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">')
        add(f'<defs><clipPath id="card-clip"><rect width="{width}" height="{height}" rx="{SCOREBUG_RADIUS}"/></clipPath></defs>')
        add('<g clip-path="url(#card-clip)">')
        add(_svg_rect(0, 0, width, height, '#f7f7f8'))

        # Logo cells (white) + score cells. Each team's own primary color is
        # the score cell's background, with the text color picked
        # automatically for contrast (get_text_color), so a team with a
        # light color doesn't end up with unreadable white-on-white.
        add(_svg_rect(0, 0, SCOREBUG_LOGO_COL_W, top_h, 'white'))
        add(_svg_line(0, SCOREBUG_ROW_H, SCOREBUG_LOGO_COL_W, SCOREBUG_ROW_H))
        add(_svg_logo_tag(self.away.image, 4, 4, SCOREBUG_LOGO_COL_W - 8, SCOREBUG_ROW_H - 8))
        add(_svg_logo_tag(self.home.image, 4, SCOREBUG_ROW_H + 4, SCOREBUG_LOGO_COL_W - 8, SCOREBUG_ROW_H - 8))

        away_bg = self.away.primary_color or SCOREBUG_ACCENT
        home_bg = self.home.primary_color or SCOREBUG_ACCENT
        away_text = _svg_hex(get_text_color(away_bg))
        home_text = _svg_hex(get_text_color(home_bg))
        add(_svg_rect(SCOREBUG_LOGO_COL_W, 0, SCOREBUG_SCORE_COL_W, SCOREBUG_ROW_H, _svg_hex(away_bg)))
        add(_svg_rect(SCOREBUG_LOGO_COL_W, SCOREBUG_ROW_H, SCOREBUG_SCORE_COL_W, SCOREBUG_ROW_H, _svg_hex(home_bg)))
        add(_svg_line(SCOREBUG_LOGO_COL_W, SCOREBUG_ROW_H, left_w, SCOREBUG_ROW_H))

        score_right = left_w - 16
        code_left = SCOREBUG_LOGO_COL_W + 14
        add(_svg_text(code_left, SCOREBUG_ROW_H / 2 + 10, self.away.code.upper()[:3], FONT_SIZE_SCOREBUG_CODE, away_text, opacity='0.9'))
        add(_svg_text(score_right, SCOREBUG_ROW_H / 2 + 16, away_score, FONT_SIZE_SCOREBUG_SCORE, away_text, anchor='end'))
        add(_svg_text(code_left, SCOREBUG_ROW_H + SCOREBUG_ROW_H / 2 + 10, self.home.code.upper()[:3], FONT_SIZE_SCOREBUG_CODE, home_text, opacity='0.9'))
        add(_svg_text(score_right, SCOREBUG_ROW_H + SCOREBUG_ROW_H / 2 + 16, home_score, FONT_SIZE_SCOREBUG_SCORE, home_text, anchor='end'))

        # Right panel: inning top-right (both arrows always drawn, only the
        # active one lit), bases diamond + outs dots centered underneath,
        # count to the right.
        add(_svg_rect(left_w, 0, SCOREBUG_RIGHT_W, top_h, _svg_hex(SCOREBUG_ACCENT)))
        add(_svg_line(left_w, 0, left_w, top_h))

        inning_right = width - 22
        inning_y = 42
        arrow_w, arrow_h = 15, 11
        arrow_x = inning_right - 64
        top_color, top_opacity = ('#ffb53a', 1) if self.inning_top else ('white', 0.3)
        bottom_color, bottom_opacity = ('#ffb53a', 1) if not self.inning_top else ('white', 0.3)
        top_apex_y = inning_y - 15 - arrow_h
        add(f'<polygon points="{arrow_x},{inning_y - 15} {arrow_x + arrow_w},{inning_y - 15} {arrow_x + arrow_w / 2},{top_apex_y}" '
            f'fill="{top_color}" fill-opacity="{top_opacity}"/>')
        bottom_apex_y = inning_y - 3 + arrow_h
        add(f'<polygon points="{arrow_x},{inning_y - 3} {arrow_x + arrow_w},{inning_y - 3} {arrow_x + arrow_w / 2},{bottom_apex_y}" '
            f'fill="{bottom_color}" fill-opacity="{bottom_opacity}"/>')
        add(_svg_text(inning_right, inning_y, self.inning, FONT_SIZE_SCOREBUG_INNING, '#ffb53a', anchor='end'))

        # Bases: 3 equal diamonds spaced along the same diagonal axes (so
        # they stay visually "aligned"), offset by more than their radius so
        # there's a gap between them instead of a shared edge.
        dcx = left_w + SCOREBUG_RIGHT_W / 2 - 25  # nudged left so bases+outs sit centered in their own space
        dr = SCOREBUG_DIAMOND_RADIUS
        offset = dr + SCOREBUG_DIAMOND_GAP
        second_center = (dcx, 56)                                              # 2nd: top
        first_center = (second_center[0] + offset, second_center[1] + offset)  # 1st: lower-right
        third_center = (second_center[0] - offset, second_center[1] + offset)  # 3rd: lower-left
        bases = ((first_center, self.runner1), (second_center, self.runner2), (third_center, self.runner3))
        for (bx, by), occupied in bases:
            points = _svg_diamond_points(bx, by, dr)
            if occupied:
                add(f'<polygon points="{points}" fill="#ffb53a"/>')
            else:
                add(f'<polygon points="{points}" fill="none" stroke="white" stroke-opacity="0.8" stroke-width="2.5"/>')

        pyramid_bottom = first_center[1] + dr  # bottom vertex of 1st/3rd (lowest point of the shape)
        outs_y = pyramid_bottom + 21
        dot_gap = 22
        for i in range(2):
            filled = i < self.outs
            dot_x = dcx - dot_gap / 2 + i * dot_gap
            dot_fill = '#ffb53a' if filled else 'none'
            add(f'<circle cx="{dot_x}" cy="{outs_y}" r="{SCOREBUG_BASE_MARKER_RADIUS}" fill="{dot_fill}" stroke="white" stroke-width="2"/>')

        count = f'{self.balls}-{self.strikes}'
        add(_svg_text(width - 16, outs_y + 12, count, FONT_SIZE_SCOREBUG_COUNT, 'white', anchor='end'))

        # Bottom pitcher strip
        add(_svg_line(0, top_h, width, top_h))
        add(_svg_text(16, top_h + SCOREBUG_PITCHER_ROW_H / 2 + 8, pitcher_lastname, FONT_SIZE_SCOREBUG_PITCHER, '#1a1a1e'))
        add(_svg_text(width - 16, top_h + SCOREBUG_PITCHER_ROW_H / 2 + 7, f'P:{pitcher_pitches}', FONT_SIZE_SCOREBUG_PITCH_COUNT, '#8a8a90', anchor='end'))

        add('</g>')
        add(f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="{SCOREBUG_RADIUS}" fill="none" stroke="{CARD_BORDER_COLOR}" stroke-width="3"/>')
        add('</svg>')

        png_bytes = cairosvg.svg2png(bytestring=''.join(parts).encode())
        return Image.open(BytesIO(png_bytes)).convert('RGBA')

    def make_overlay(self):
        image = Image.new('RGBA', self.resolution)
        if not self.game_started:
            for team_logo in [('away_logo', 3.70), ('home_logo', 0.30)]:
                try:
                    logo = Image.open(BytesIO(requests.get(self.game_info.get(team_logo[0]), timeout=TIMEOUT).content))
                    width, height = logo.size
                    logo = logo.resize((int(self.resolution[0] / 5.00), int((self.resolution[0] / 5.00) * height / width)))
                    try:
                        image.paste(logo, (int(team_logo[1] * self.resolution[0] / 5.00), int(self.resolution[1] / 1.50)), logo)
                    except:
                        image.paste(logo, (int(team_logo[1] * self.resolution[0] / 5.00), int(self.resolution[1] / 1.50)))
                except:
                    logger.error('Could not generate team initial logo')

        elif self.current_play > 1:
            scorebug = self.get_scorebug()
            ratio = self.resolution[0] / scorebug.size[0] / SCOREBUG_DISPLAY_SCALE
            scorebug = scorebug.resize((int(scorebug.size[0] * ratio), int(scorebug.size[1] * ratio)))
            image.paste(scorebug, (20, self.resolution[1] - scorebug.size[1] - 20), scorebug)
            if self.inning != 'F':
                player = self.get_current_batter()
                ratio = self.resolution[0] / player.size[0] / 2
                player = player.resize((int(player.size[0] * ratio), int(player.size[1] * ratio)))
                image.paste(player, (self.resolution[0] - player.size[0] - 30, self.resolution[1] - player.size[1] - 30), player)
        elif self.current_play <= 1:
            home_lineup = self.get_lineup(self.home, HOME_NAME)
            away_lineup = self.get_lineup(self.away, AWAY_NAME)
            ratio = self.resolution[0] / home_lineup.size[0] / 2.8
            home_lineup = home_lineup.resize((int(home_lineup.size[0] * ratio), int(home_lineup.size[1] * ratio)))
            away_lineup = away_lineup.resize((int(away_lineup.size[0] * ratio), int(away_lineup.size[1] * ratio)))
            image.paste(home_lineup, (int(self.resolution[0] / 2 + 100), 100), home_lineup)
            image.paste(away_lineup, (int(self.resolution[0] / 2 - 100 - away_lineup.size[0]), 100), away_lineup)

        image.save(os.path.join(WORKING_DIR, 'overlay-tmp.png'), "PNG")
        os.replace(os.path.join(WORKING_DIR, 'overlay-tmp.png'), os.path.join(WORKING_DIR, 'overlay.png'))

    def start_video_file(self, file, duration=None):
        command = [
            'ffmpeg',
            '-re',
            '-i', file,
            '-c:a', 'aac',
            '-b:a', '128k',
            '-strict', 'experimental',
            '-f', 'flv',
            '-b:v', '8000k',
            '-vcodec', 'h264',
            '-preset', 'ultrafast',
            '-g', '60',
            '-s', '1920x1080',
        ]
        video_file_proc = subprocess.Popen(command + [f'{MAIN_STREAM}'], stdin=subprocess.PIPE, stderr=self.logfile or subprocess.STDOUT, universal_newlines=True)
        if duration:
            time.sleep(duration)
            video_file_proc.terminate()
        else:
            video_file_proc.wait()

    def initialize_stream(self, restart=False):
        if config.has_option('baseball', 'intro_file') and not restart:
            logger.info("Starting intro file.")
            self.start_video_file(config.get('baseball', 'intro_file'))

        FINE_TUNE = FINE_TUNE_CAMERA_FIELD1 if self.game_info.get('camera') == 'camera1' else FINE_TUNE_CAMERA_FIELD2
        command = [
            'ffmpeg',
            '-re',
            '-thread_queue_size', '512',
            '-rtsp_transport', 'tcp',
            '-i', INPUT_CAMERA_STREAM_FIELD1 if self.game_info.get('camera') == 'camera1' else INPUT_CAMERA_STREAM_FIELD2,
            '-f', 'image2',
            '-framerate', '3',
            '-loop', '1',
            '-i', os.path.join(WORKING_DIR, 'overlay.png'),
            '-filter_complex', '[0:v]%sscale=%s:%s[scaled];[scaled][1:v]overlay[outv]' % (FINE_TUNE, INPUT_RESOLUTION[0], INPUT_RESOLUTION[1]),
            '-map', '[outv]',
            '-map', '0:a',
            '-c:a', 'aac',
            '-b:a', '96k',
            '-strict', 'experimental',
            '-rtbufsize', '1G',
            '-f', 'flv',
            '-b:v', '3000k',
            '-vcodec', 'h264',
            '-preset', 'ultrafast',
            '-g', '60',
            '-s', '1920x1080',
        ]
        # watchdog: ffmpeg appends its encoding progress (frame=/out_time=) to this
        # file every ~500ms; if the file stops growing, the output is stalled
        # (typically a frozen RTMP socket after a connection loss), see loop_check_stream
        try:
            os.remove(self.progress_file)
        except OSError:
            pass
        self.last_progress = -1
        self.last_progress_time = time.time()
        self.stream_started_at = time.time()
        main_command = command + ['-progress', self.progress_file, f'{MAIN_STREAM}']
        logger.info('FFMPEG Command: %s', ' '.join(main_command))
        self.stream_proc = subprocess.Popen(main_command, stdin=subprocess.PIPE, stderr=self.logfile or subprocess.STDOUT, universal_newlines=True)
        if BACKUP_STREAM and not restart:
            self.backup_proc = subprocess.Popen(command + [f'{BACKUP_STREAM}'], stdin=subprocess.PIPE, stderr=self.logfile or subprocess.STDOUT, universal_newlines=True)

    def restart_stream(self):
        """Kill the current ffmpeg process and start a new one.

        Triggered by:
        - the local watchdog (loop_check_stream) when ffmpeg exited or is
          frozen (no output progress), e.g. after a connection loss;
        - the server (odoo) through the `stream_restart_count` value of
          /game/current_score when the YouTube API reports that the bound
          live stream is no longer receiving data.
        """
        # detach the proc first so loop_check_stream does not also restart it
        proc, self.stream_proc = self.stream_proc, None
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
        self.initialize_stream(restart=True)

    def loop_check_stream(self):
        while True:
            if self.force_end:
                break
            if self.stream_proc:
                retcode = self.stream_proc.poll()
                if retcode is not None:
                    # also restart on exit code 0: a "clean" exit in the middle of
                    # a game usually means the RTMP server closed the connection
                    logger.info('FFmpeg exited (return code %s), restarting', retcode)
                    self.restart_stream()
                elif time.time() - self.stream_started_at > STREAM_STARTUP_GRACE:
                    # watchdog: after a connection loss ffmpeg often survives in a
                    # frozen state (TCP retransmits silently, rtbufsize absorbs)
                    # without ever exiting; detect it via the progress file
                    try:
                        progress = os.path.getsize(self.progress_file)
                    except OSError:
                        progress = -1
                    if progress != self.last_progress:
                        self.last_progress = progress
                        self.last_progress_time = time.time()
                    elif time.time() - self.last_progress_time > STREAM_STALL_TIMEOUT:
                        logger.info('FFmpeg is stalled (no progress for %ss), restarting', STREAM_STALL_TIMEOUT)
                        self.restart_stream()
            time.sleep(1)

    def loop_check_main_website(self):
        error = 0
        while True:
            if self.force_end:
                break
            time.sleep(15)
            try:
                current_score = requests.get(f'{WEBSITE_URL}/game/current_score', timeout=30)
                current_score.raise_for_status()
                current_score = current_score.json()
                logger.info('Got current score %s', current_score)
            except (requests.RequestException, ValueError):
                # connection loss included: keep the loop alive, ffmpeg will
                # reconnect by itself or be restarted by the server on recovery
                logger.exception('Could not get current score')
                continue
            if not current_score.get('game'):
                error += 1
                logger.info('No game detected on the website')
                if error >= 3:
                    logger.info('Force stoppping game')
                    self.force_end = True
            else:
                error = 0
                restart_count = current_score.get('stream_restart_count') or 0
                if self.stream_restart_count is None:
                    self.stream_restart_count = restart_count
                elif restart_count != self.stream_restart_count:
                    self.stream_restart_count = restart_count
                    logger.info('Stream restart requested by the server (count %s)', restart_count)
                    self.restart_stream()

    def loop_main(self):
        start = int(time.time() * 1000)
        self.make_overlay()
        time.sleep(10)
        end_time = None
        while True:
            if self.force_end:
                break
            if not self.game_started:
                self.init_game()
                logger.info('Game has not started yet, waiting 30s then retrying')
                time.sleep(30)
                continue
            logger.info('Play %s', self.current_play)
            if self.inning == 'F':
                if self.mode == 'replay':
                    self.force_end = True
                if not end_time:
                    end_time = time.time()
                elif time.time() - end_time > 120:
                    self.force_end = True
            else:
                end_time = None
            if self.mode == 'replay' and self.replay_mode == 'realtime':
                current_time = self.beginning + (int(time.time() * 1000) - start)
                while self.play_time < current_time:
                    data = self.session.get(PLAY_URL % (self.id, self.current_play), headers=HEADERS, timeout=TIMEOUT)
                    try:
                        data = data.json()
                    except Exception:
                        self.current_play += 1
                        continue
                    self.update_game(data)
                    logger.info('Play %s', self.current_play)
                    self.make_overlay()
                    self.current_play += 1
                time.sleep(0.5)
            elif self.mode == 'replay' and self.replay_mode == 'sequence':
                data = self.session.get(PLAY_URL % (self.id, self.current_play), headers=HEADERS, timeout=TIMEOUT)
                try:
                    data = data.json()
                except:
                    self.current_play += 1
                    continue
                self.update_game(data)
                self.make_overlay()
                self.current_play += 1
                time.sleep(2)
            elif self.mode == 'live':
                last_play = self.session.get(LATEST_PLAY_URL % self.id, headers=HEADERS, timeout=TIMEOUT)
                last_play = int(last_play.json())
                if self.current_play == last_play:
                    time.sleep(1)
                    continue
                self.current_play = last_play
                data = self.session.get(PLAY_URL % (self.id, last_play), headers=HEADERS, timeout=TIMEOUT)
                try:
                    data = data.json()
                except:
                    continue
                self.update_game(data)
                self.make_overlay()
                time.sleep(3)
        self.cleanup()
        if config.has_option('baseball', 'end_file'):
            logger.info("Starting end file. %s", config.get('baseball', 'end_file'))
            self.start_video_file(config.get('baseball', 'end_file'))

    def cleanup(self):
        logger.info("Cleaning up...")
        self.force_end = True
        if hasattr(self, 'stream_proc') and self.stream_proc and self.stream_proc.poll() is None:
            self.stream_proc.kill()
            logger.info("FFmpeg process terminated.")
        if hasattr(self, 'backup_proc') and self.backup_proc and self.backup_proc.poll() is None:
            self.backup_proc.kill()
            logger.info("Backup FFmpeg process terminated.")
        if self.logfile:
            self.logfile.close()
        if os.path.exists(os.path.join(WORKING_DIR, 'default.png')):
            shutil.copyfile(os.path.join(WORKING_DIR, 'default.png'), os.path.join(WORKING_DIR, 'overlay.png'))

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename=LOGFILE, filemode='a')
    logger.info('Starting service')
    while True:
        try:
            current_score = requests.get(f'{WEBSITE_URL}/game/current_score', timeout=30)
            current_score.raise_for_status()
            current_score = current_score.json()
            logger.info('Got current score %s', current_score)
        except (requests.RequestException, ValueError):
            logger.exception('Could not get current score')
            current_score = {}
        if current_score.get('game') and current_score.get('live_score_id') and current_score.get('youtube_video_id'):
            logger.info('Found game %s - starting stream', current_score.get('live_score_id'))
            try:
                replay_mode = config.has_option('baseball', 'replay_mode') and config.get('baseball', 'replay_mode')
                game = Game(current_score, mode=config.get('baseball', 'mode'), replay_mode=replay_mode)

                def signal_handler(sig, frame):
                    logger.info("Signal received: %s", sig)
                    game.cleanup()
                    if game.logfile:
                        game.logfile.close()
                    os._exit(0)
                signal.signal(signal.SIGINT, signal_handler)
                signal.signal(signal.SIGTERM, signal_handler)
                greenlet_loop_main = gevent.spawn(game.loop_main)
                greenlet_loop_check_stream = gevent.spawn(game.loop_check_stream)
                greenlet_loop_check_main_website = gevent.spawn(game.loop_check_main_website)
                gevent.joinall([greenlet_loop_main, greenlet_loop_check_stream, greenlet_loop_check_main_website])
            except Exception:
                logger.exception('Failed to start game, retrying in 60s')
                time.sleep(60)

        time.sleep(60)


if __name__ == "__main__":
    main()
