import json,re
import time
import requests
from PIL import Image, ImageDraw, ImageFont


# msg vs beveren
GAMEID = 126549
BASE_URL = 'https://game.wbsc.org/gamedata'
LATEST_PLAY_URL = '%s/%%s/latest.json' % (BASE_URL)
PLAY_URL = '%s/%%s/play%%s.json' % (BASE_URL)
HEADERS = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.97 Safari/537.36"}

class Player(object):
    def __init__(self, player_data, lineupcode):
        self.id = player_data.get('playerid')
        self.team_id = player_data.get('teamid')
        self.name = player_data.get('name')
        self.image_url = player_data.get('image')
        self.stats = player_data.get('SEASON')
        self.update(player_data, lineupcode)

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

class Team(object):
    def __init__(self, id, code, players):
        self.id = id
        self.code = code
        self.lineup = {
            players[lineupcode]['playerid']: Player(players[lineupcode], lineupcode)
            for lineupcode in players
            if players[lineupcode]['teamid'] == id and lineupcode[2] != '0'
        }
        player, lineupcode = [(p, lineupcode) for lineupcode, p in players.items() if (p.get('POS') == 'P' or p.get('PITCHIP')) ][0]
        self.pitcher = Player(player, lineupcode)

    def update(self, data):
        for lineupcode, player in data.items():
            if player.get('teamid') != self.id:
                continue
            if lineupcode[2] != '0':
                if player.get('playerid') in self.lineup:
                    self.lineup[player['playerid']].update(player, lineupcode)
                else:
                    self.lineup[player['playerid']] = Player(player, lineupcode)
            elif player.get('POS') == 'P' or player.get('PITCHIP'):
                # TODO: multiple pitchers?
                if player.get('playerid') == self.pitcher.id:
                    self.pitcher.update(player, lineupcode)
                else:
                    self.pitcher = Player(player, lineupcode)

class Game(object):
    def __init__(self, gameid, mode='live', replay_mode=None):
        self.id = gameid
        self.session = requests.Session()
        data = self.session.get(PLAY_URL % (gameid, 1), headers=HEADERS)
        data = data.json()
        self.mode = mode
        self.replay_mode = replay_mode
        self.beginning = int(data.get('playdata')[0].get('t'))
        self.data = data
        home_id = data.get('eventhomeid')
        away_id = data.get('eventawayid')
        players = data.get('boxscore')
        self.home = Team(home_id, data.get('eventhome'), players)
        self.away = Team(away_id, data.get('eventaway'), players)
        self.update_game(data)

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
        self.play_time =  int(data.get('playdata')[0].get('t'))
        self.runner1 = data.get('situation').get('runner1')
        self.runner2 = data.get('situation').get('runner2')
        self.runner3 = data.get('situation').get('runner3')
        self.outs = data.get('situation').get('outs')
        self.balls = data.get('situation').get('balls')
        self.strikes = data.get('situation').get('strikes')

    def make_scorebug(self):
        home_color = "Navy"
        away_color = "Black"
        home_score = str(self.score_home)
        away_score = str(self.score_away)

        bg_color = (0, 0, 0, 128)
        text_color = (255, 255, 255, 200)
        out_color = (255, 255, 255, 100)
        border_base_color = (255, 255, 255, 128)
        base_runner_color = (255, 255, 128, 255)
        image = Image.new('RGBA',(1000, 750))

        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(((0, 0), (1000, 120)), radius=30, fill=bg_color)
        draw.rounded_rectangle(((0, 140), (1000, 260)), radius=30, fill=bg_color)
        draw.rounded_rectangle(((0, 300), (1000, 750)), radius=30, fill=bg_color)
        draw.rectangle(((0, 300), (600, 450)), fill=away_color)
        draw.rectangle(((0, 450), (600, 600)), fill=home_color)

        l1 = 156
        l2 = 71
        offset_x = 650
        offset_y = 320
        first_base = base_runner_color if self.runner1 else None
        second_base = base_runner_color if self.runner2 else None
        thrid_base = base_runner_color if self.runner3 else None

        # bases
        draw.polygon([
            (l1+l1-l2+offset_x, l1-l2+offset_y),
            (l1+l1+offset_x, l1+offset_y),
            (l1+l1-l2+offset_x, l1+l2+offset_y),
            (l1+l1-l2-l2+offset_x, l1+offset_y),
            ], fill=first_base, outline =border_base_color, width=10)
        draw.polygon([
            (l1+offset_x, 0+offset_y),
            (l1+l2+offset_x, l2+offset_y),
            (l1+offset_x, l2+l2+offset_y),
            (l1-l2+offset_x, l2+offset_y),
            ], fill=second_base, outline=border_base_color, width=10)
        draw.polygon([
            (l2+offset_x, l1-l2+offset_y),
            (l2+l2+offset_x, l1+offset_y),
            (l2+offset_x, l1+l2+offset_y),
            (0+offset_x, l1+offset_y),
            ], fill=thrid_base, outline =border_base_color, width=10)

        font_team = ImageFont.truetype('/usr/share/fonts/X11/Type1/c0632bt_.pfb',120)
        font_name = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',60)
        font_out = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',100)
        font_score = ImageFont.truetype('/usr/share/fonts/X11/Type1/c0632bt_.pfb',120)

        away_score_length = draw.textlength(away_score,font_score)
        home_score_length = draw.textlength(home_score,font_score)
        draw.text((20, 300), self.away.code.upper()[:3],fill=text_color, font=font_team)
        draw.text((500-away_score_length, 300), away_score,fill=text_color, font=font_score)
        draw.text((20, 450), self.home.code.upper()[:3],fill=text_color, font=font_team)
        draw.text((500-home_score_length, 450), home_score,fill=text_color, font=font_score)
        lineup_order = '%s.' % self.batter.batting_order
        lineup_order_length = draw.textlength(lineup_order,font_name)
        draw.text((lineup_order_length+20, 7), self.pitcher.name,fill=text_color, font=font_name)
        draw.text((800, 7), 'P: %s' % self.pitcher.pitches,fill=text_color, font=font_name)
        draw.text((lineup_order_length+20, 147), self.batter.name,fill=text_color, font=font_name)
        draw.text((10, 147), lineup_order,fill=text_color, font=font_name)
        # inning
        draw.polygon([(70, 675), (70+40, 675), (70+20, 675+ (-40 if self.inning_top else 40)), ], fill=border_base_color)
        draw.text((70+15+40, 600), self.inning,fill=text_color, font=font_team)
        draw.text((330, 600), str(self.outs),fill=text_color, font=font_team)
        draw.text((400, 600), 'Out',fill=out_color, font=font_out)
        count = '%s-%s' % (self.balls, self.strikes)
        count_length = draw.textlength(count,font_team)
        draw.text((l1+offset_x - count_length/2, 600), count,fill=text_color, font=font_team)
        image.save("scorebug.png","PNG")


    def run(self):
        current_play = 1
        start = int(time.time()*1000)
        while True:
            print('Play', current_play)
            if self.inning == 'F':
                break
            if self.mode == 'replay' and self.replay_mode == 'realtime':
                current_time = self.beginning + (int(time.time()*1000) - start)
                while game.play_time < current_time:
                    current_play += 1
                    data = self.session.get(PLAY_URL % (self.id, current_play), headers=HEADERS)
                    try:
                        data = data.json()
                    except:
                        current_play += 1
                        continue
                    game.update_game(data)
                    print('play', current_play)
                    game.make_scorebug()
                time.sleep(0.5)
            elif self.mode == 'replay' and self.replay_mode == 'sequence':
                data = self.session.get(PLAY_URL % (self.id, current_play), headers=HEADERS)
                try:
                    data = data.json()
                except:
                    current_play += 1
                    continue
                game.update_game(data)
                game.make_scorebug()
                current_play += 1
                time.sleep(0.5)
            elif self.mode == 'live':
                last_play = self.session.get(LATEST_PLAY_URL % self.id, headers=HEADERS)
                last_play = int(last_play.json())
                data = self.session.get(PLAY_URL % (self.id, last_play), headers=HEADERS)
                try:
                    data = data.json()
                except:
                    current_play += 1
                    continue
                game.update_game(data)
                game.make_scorebug()
                time.sleep(0.5)


game = Game(GAMEID, mode='live', replay_mode='sequence')
game.run()
