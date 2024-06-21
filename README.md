# baseball-streaming (work in progress)
python script to fetch data from WBSC games and stream from a single camera with a rtsp stream to a rtmp youtube stream. It is designed to run as service.

The data fetching was inspired by https://github.com/keero/baseball-streaming/ but I'm a python developer ¯\_(ツ)_/¯

The script fetches data from a given website to see if a game is to be streamed.
When the game starts it polls [wbsc.org](https://game.wbsc.org/gamedata) to generate the overlay and stream to youtube.

## Stream

The stream uses ffmpeg and is designed to be run on a raspberry pi 5 on the same network as the camera.

## CLI


```bash
python generate_scoreboard.py /path/to/config/file
```

## Configuration

The configuration file should contain
```bash
[baseball]
website_url = domain to website to retrieve the data
working_dir = working directirey where the overlay will be saved
logfile = if defined use log file else use stdout
input_stream_1 = rtsp camera 1
input_stream_2 = rtsp camera 2
main_rtmp_stream = main rtmp stream (rtmp://a.rtmp.youtube.com/live2/STREAMKEY)
backup_rtmp_stream = if defined use as backup rtmp stream (rtmp://b.rtmp.youtube.com/live2?backup=1/STREAMKEY)
mode = realtime|replay


```
The website website_url is supposed to have a route /game/current_score which return a json containing
```yaml
    {
    'game': True,
    'game_id': game id from  wbsc,
    'live_score_id': game id from  wbsc,
    'youtube_video_id': non null value to stream,
    'camera': rec.game_id.division.camera or 'camera1',
    'home_team': home team name,
    'away_team': away team name,
    'home_logo': url to the home logo,
    'home_primary_color': hexadecimal primary color for home team,
    'home_secondary_color': hexadecimal secondary color for home team,
    'away_logo': url to the away logo,
    'away_primary_color': hexadecimal primary color for away team,
    'away_secondary_color': hexadecimal secondary color for away team,
}
```

It still a work in progress and resulting video can be seen on
https://www.youtube.com/@msgphoenix9045/streams

## TODO:
- Robustness
- Add voice on another channel
- Add adverts images from url image
- Add data and statistics
