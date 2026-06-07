# Headed-minimized Selenium probe

## Google headed-minimized: 4 / 5 clean
- `site:linkedin.com/in/ cornell founder` -> BAD (externals=0, captcha=True, on_sorry=True)
- `Ava Labs founder cornell university` -> OK (externals=84, captcha=False, on_sorry=False)
- `Hyro company linkedin` -> OK (externals=15, captcha=False, on_sorry=False)
- `"Cornell University" startup AI funding 2024` -> OK (externals=46, captcha=False, on_sorry=False)
- `Cornell entrepreneurship eship` -> OK (externals=16, captcha=False, on_sorry=False)

## LinkedIn headed-minimized: 3 / 3 returned headlines
- nanit: OK (6 headlines, 1,890,034 bytes)
- reid-hoffman: OK (8 headlines, 1,523,908 bytes)
- cornell-edu: OK (6 headlines, 1,957,765 bytes)

## Headless control
- Same query as Google #1, headless: captcha=True, on_sorry=True, externals=0
