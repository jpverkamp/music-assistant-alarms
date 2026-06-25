# `music-assistant-alarm-scheduler`

A script designed to read a Google Sheet (published as a CSV), parse it as best as possible into a series of timed music events, and play them back via Music Assistant. 

It's not really designed for anyone but me to use, but hopefully you can take this and adapt it to your own uses. 

## Environment variables

- `MASS_TOKEN` - A long lived Music Assistant token
- `MASS_URL` - Where Music Assistant lives
- `TZ` - The timezone the start and end times are defined in
- `ALARMS_URL` - A published as CSV Google Sheet with alarms; technically this can be any CSV I guess

## Google Sheet columns

- `Days` - What days of the week the script runs on, like "Mon,Tues,Wed" or "Mon-Fri"
- `Start Time` - The HH:MM AM/PM to start the playlist
- `End Time` - Same for ending
- `Playlist` - The playlist (or album or radio station) to play
- `Volume` - The volume to set the speaker group to
- `Speakers` - A list of speakers by name/display name
- `When?` - TODO: What time of year this should play (overrides the above) (not implemented yet)
- `Shuffle?` - If the playlist should shuffle
