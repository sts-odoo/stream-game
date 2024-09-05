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
import sys
import time
from PIL import Image, ImageDraw, ImageFont, ImageOps
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
FINE_TUNE_CAMERA_FIELD1 = 'rotate=0.06,crop=2320:1080:150:100,'
FINE_TUNE_CAMERA_FIELD2 = ''

FONTS = '/usr/share/fonts/X11/Type1/NimbusSans-Regular.pfb'

MAIN_STREAM = config.get('baseball', 'main_rtmp_stream')
BACKUP_STREAM = config.has_option('baseball', 'backup_rtmp_stream') and config.get('baseball', 'backup_rtmp_stream')

LOGFILE = config.has_option('baseball', 'logfile') and config.get('baseball', 'logfile')
INPUT_RESOLUTION = (2560, 1440)

PHOTO_WIDTH = 470

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
            self.image = Image.open(BytesIO(self.game.session.get(self.image_url).content))
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
        player, lineupcode = [(p, lineupcode) for lineupcode, p in players.items() if (p.get('POS') == 'P' or p.get('PITCHIP'))][0]
        self.pitcher = Player(self.game, self, player, lineupcode)
        self.primary_color = primary_color
        self.secondary_color = secondary_color
        self.image = False
        if logo_url:
            self.image = Image.open(BytesIO(self.game.session.get(logo_url).content))

    def update(self, data):
        for lineupcode, player in data.items():
            if player.get('teamid') != self.id:
                continue
            if lineupcode[2] != '0':
                if player.get('playerid') in self.lineup:
                    self.lineup[player['playerid']].update(player, lineupcode)
                else:
                    self.lineup[player['playerid']] = Player(self.game, self, player, lineupcode)
            elif player.get('POS') == 'P' or player.get('PITCHIP'):
                # TODO: multiple pitchers?
                if player.get('playerid') == self.pitcher.id:
                    self.pitcher.update(player, lineupcode)
                else:
                    self.pitcher = Player(self.game, self, player, lineupcode)

    def get_lineup(self):
        lineup = sorted([player for x, player in self.lineup.items()], key= lambda p: p.batting_order)
        if self.pitcher not in lineup:
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
        self.game_info = game_info
        self.logfile = open(LOGFILE, 'a') if LOGFILE else None
        self.initialize_stream()
        self.init_game()

    def init_game(self):
        try:
            self.session = requests.Session()
            if self.mode == 'live':
                last_play = self.session.get(LATEST_PLAY_URL % self.id, headers=HEADERS)
                last_play = int(last_play.json())
                self.current_play = last_play
                data = self.session.get(PLAY_URL % (self.id, last_play), headers=HEADERS)
                data = data.json()
            else:
                self.current_play = 1
                data = self.session.get(PLAY_URL % (self.id, self.current_play), headers=HEADERS)
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
        self.pitcher = self.home.pitcher if self.home.pitcher.id == pitcherid else self.away.pitcher
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
        self.batter.team.primary_color
        main_color = self.batter.team.primary_color
        second_color = "White"
        third_color = self.batter.team.secondary_color
        text_main_color = get_text_color(main_color)
        text_second_color = "Black"
        image = Image.new('RGBA', (2500, 500))
        draw = ImageDraw.Draw(image)
        draw.polygon([(250, 100), (2500, 100), (2400, 250), (250, 250)], fill=main_color)
        draw.polygon([(250, 250), (2400, 250), (2300, 400), (250, 400)], fill=second_color)
        if self.batter.image:
            draw.ellipse((0, 0) + (500, 500), fill=third_color)
        font_name = ImageFont.truetype(FONTS,80)
        font_stat = ImageFont.truetype(FONTS,60)
        draw.text((550, 120), '%s. %s - %s' % (self.batter.batting_order, self.batter.name, self.batter.position), fill=text_main_color, font=font_name)
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
        draw.text((550, 270), player_label, fill=text_second_color, font=font_stat)
        if self.batter.image:
            image.paste(self.batter.image, (15, 15), self.batter.image)
        return image

    def get_lineup(self, team, filename):
        font_team = ImageFont.truetype(FONTS, 60)
        font_name = ImageFont.truetype(FONTS, 30)
        bg_color = team.primary_color + (220,)
        pitcher_color = team.secondary_color + (220,)
        text_main_color = get_text_color(bg_color)
        text_pitcher_color = get_text_color(pitcher_color)
        height = 50
        width = 500
        width2 = 450
        space = 10
        logo_height = 100
        image = Image.new('RGBA', (width, logo_height + (height + space) * 10))
        draw = ImageDraw.Draw(image)

        draw.polygon([(space, space), (width - space, space), (width - space, logo_height - space), (space, logo_height - space)], fill=bg_color)
        draw.text((150, 10), filename.upper(), fill=text_main_color, font=font_team)

        position = logo_height

        for player in team.get_lineup():
            color = bg_color
            text_color = text_main_color
            if player.batting_order == '0':
                color = pitcher_color
                text_color = text_pitcher_color

            draw.polygon([(0, position), (width, position), (width2, height + position), (0, height + position)], fill=color)
            if player.batting_order != '0':
                draw.text((10, position), player.batting_order, fill=text_color, font=font_name)
            player_name = '%s %s.' % (player.lastname.upper(), player.firstname[0])
            for font_size in reversed(range(30)):
                font_player = ImageFont.truetype(FONTS, font_size)
                player_name_length = draw.textlength(player_name, font_player)
                if player_name_length < 350:
                    break
            draw.text((50, position), player_name, fill=text_color, font=font_player)
            draw.text((400, position), player.position, fill=text_color, font=font_name)
            position += space + height

        width, height = team.image.size
        team_image = team.image.resize((int((logo_height - 3 * space) * width / height), (logo_height - 3 * space)))
        try:
            image.paste(team_image, (int(space * 1.5), int(space * 1.5)), team_image)
        except:
            image.paste(team_image, (int(space * 1.5), int(space * 1.5)))
        return image

    def get_scorebug(self):
        home_score = str(self.score_home)
        away_score = str(self.score_away)

        bg_color = (0, 0, 0, 180)
        text_color = (255, 255, 255, 200)
        border_base_color = (255, 255, 255, 170)
        base_runner_color = (255, 255, 128, 255)
        image = Image.new('RGBA', (1000, 750))

        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(((0, 140), (1000, 260)), radius=30, fill=bg_color)
        draw.rounded_rectangle(((0, 300), (1000, 750)), radius=30, fill=bg_color)
        draw.rectangle(((0, 300), (600, 450)), fill=self.away.primary_color)
        draw.rectangle(((0, 450), (600, 600)), fill=self.home.primary_color)

        l1 = 156
        l2 = 71
        offset_x = 650
        offset_y = 320
        first_base = base_runner_color if self.runner1 else None
        second_base = base_runner_color if self.runner2 else None
        thrid_base = base_runner_color if self.runner3 else None

        # bases
        draw.polygon([
            (l1 + l1 - l2 + offset_x, l1 - l2 + offset_y),
            (l1 + l1 + offset_x, l1 + offset_y),
            (l1 + l1 - l2 + offset_x, l1 + l2 + offset_y),
            (l1 + l1 - l2 - l2 + offset_x, l1 + offset_y),
            ], fill=first_base, outline=border_base_color, width=10)
        draw.polygon([
            (l1 + offset_x, 0 + offset_y),
            (l1 + l2 + offset_x, l2 + offset_y),
            (l1 + offset_x, l2 + l2 + offset_y),
            (l1 - l2 + offset_x, l2 + offset_y),
            ], fill=second_base, outline=border_base_color, width=10)
        draw.polygon([
            (l2 + offset_x, l1 - l2 + offset_y),
            (l2 + l2 + offset_x, l1 + offset_y),
            (l2 + offset_x, l1 + l2 + offset_y),
            (0 + offset_x, l1 + offset_y),
            ], fill=thrid_base, outline =border_base_color, width=10)

        font_team = ImageFont.truetype(FONTS, 120)
        font_name = ImageFont.truetype(FONTS, 60)
        font_out = ImageFont.truetype(FONTS, 100)
        font_score = ImageFont.truetype(FONTS, 120)

        away_score_length = draw.textlength(away_score, font_score)
        home_score_length = draw.textlength(home_score, font_score)
        get_text_color(self.away.primary_color)

        draw.text((20, 300), self.away.code.upper()[:3], fill=get_text_color(self.away.primary_color), font=font_team)
        draw.text((500 - away_score_length, 300), away_score, fill=get_text_color(self.away.primary_color), font=font_score)
        draw.text((20, 450), self.home.code.upper()[:3], fill=get_text_color(self.home.primary_color), font=font_team)
        draw.text((500 - home_score_length, 450), home_score, fill=get_text_color(self.home.primary_color), font=font_score)
        for font_size in reversed(range(60)):
            font_pitcher = ImageFont.truetype(FONTS, font_size)
            pitcher_name_length = draw.textlength(self.pitcher.name, font_pitcher)
            if pitcher_name_length < 700:
                break
        draw.text((20, 147), self.pitcher.name, fill=text_color, font=font_pitcher)
        draw.text((800, 147), 'P: %s' % self.pitcher.pitches, fill=text_color, font=font_name)
        # inning
        draw.polygon([(70, 675), (70 + 40, 675), (70 + 20, 675 + (-40 if self.inning_top else 40)),], fill=border_base_color)
        draw.text((70 + 15 + 40, 600), self.inning, fill=text_color, font=font_team)
        draw.text((330, 600), str(self.outs), fill=text_color, font=font_team)
        draw.text((410, 600), 'out', fill=text_color, font=font_out)
        count = '%s-%s' % (self.balls, self.strikes)
        count_length = draw.textlength(count, font_team)
        draw.text((l1 + offset_x - count_length / 2, 600), count, fill=text_color, font=font_team)
        return image

    def make_overlay(self):
        image = Image.new('RGBA', self.resolution)
        if not self.game_started:
            for team_logo in [('away_logo', 3.00), ('home_logo', 1.00)]:
                try:
                    logo = Image.open(BytesIO(requests.get(self.game_info.get(team_logo[0])).content))
                    width, height = logo.size
                    logo = logo.resize((int(self.resolution[0] / 5.00), int((self.resolution[0] / 5.00) * height / width)))
                    try:
                        image.paste(logo, (int(team_logo[1] * self.resolution[0] / 5.00), int(self.resolution[1] / 3.00)), logo)
                    except:
                        image.paste(logo, (int(team_logo[1] * self.resolution[0] / 5.00), int(self.resolution[1] / 3.00)))
                except:
                    logger.error('Could not generate team initial logo')

        elif self.current_play > 1:
            scorebug = self.get_scorebug()
            ratio = self.resolution[0] / scorebug.size[0] / 6
            scorebug = scorebug.resize((int(scorebug.size[0] * ratio), int(scorebug.size[1] * ratio)))
            image.paste(scorebug, (20, self.resolution[1] - scorebug.size[1] - 20), scorebug)

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
            '-b:a', '128k',
            '-strict', 'experimental',
            '-f', 'flv',
            '-b:v', '8000k',
            '-vcodec', 'h264',
            '-preset', 'ultrafast',
            '-g', '60',
            '-s', '1920x1080',
        ]
        logger.info('FFMPEG Command: %s', ' '.join(command + [f'{MAIN_STREAM}']))
        self.stream_proc = subprocess.Popen(command + [f'{MAIN_STREAM}'], stdin=subprocess.PIPE, stderr=self.logfile or subprocess.STDOUT, universal_newlines=True)
        if BACKUP_STREAM and not restart:
            self.backup_proc = subprocess.Popen(command + [f'{BACKUP_STREAM}'], stdin=subprocess.PIPE, stderr=self.logfile or subprocess.STDOUT, universal_newlines=True)

    def loop_check_stream(self):
        while True:
            if self.force_end:
                break
            if self.stream_proc:
                 retcode = self.stream_proc.poll()
                 if retcode:
                    logger.info('FFmpeg failed for an unknown reason (return code %s), restarting', retcode)
                    self.initialize_stream(restart=True)
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
            except requests.HTTPError:
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
                    data = self.session.get(PLAY_URL % (self.id, self.current_play), headers=HEADERS)
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
                data = self.session.get(PLAY_URL % (self.id, self.current_play), headers=HEADERS)
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
                last_play = self.session.get(LATEST_PLAY_URL % self.id, headers=HEADERS)
                last_play = int(last_play.json())
                if self.current_play == last_play:
                    time.sleep(1)
                    continue
                self.current_play = last_play
                data = self.session.get(PLAY_URL % (self.id, last_play), headers=HEADERS)
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

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename=LOGFILE, filemode='a')
    logger.info('Starting service')
    while True:
        try:
            current_score = requests.get(f'{WEBSITE_URL}/game/current_score', timeout=30)
            current_score.raise_for_status()
            current_score = current_score.json()
            logger.info('Got current score %s', current_score)
        except requests.HTTPError:
            logger.exception('Could not get current score')
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
