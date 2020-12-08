import json
import traceback
import os.path
from glob import iglob

warnings = False
errors = False

def error(fn, k, msg):
    errors = True
    print(f'ERROR! {fn}:{k} - {msg}')

def warn(fn, k, msg):
    warnings = True
    print(f'WARNING! {fn}:{k} - {msg}')

def check(file, k, obj, validation):
    for key, type_ in validation.items():
        try:
            val = obj[key]
        except KeyError:
            error(file, k, f'Key: {key} not found.')
        else:
            if not isinstance(val, type_):
                try:
                    type_(val)
                except Warning as e:
                    warn(file, k, e)
                except Exception as e:
                    error(file, k, e)

class InvalidFormatWarning(Warning):
    pass

class InvalidFormatError(Exception):
    pass


class URL(str):
    def __init__(self, data):
        if data.startswith('http://') or data.startswith('https://'):
            if 'cdn.discordapp.com' in data:
                raise InvalidFormatWarning('Discord CDN URLs are not recommended')
        else:
            raise InvalidFormatError('URL is not a well formatted url')

        super().__init__()


for fn in iglob("adventure/data/default/*.json"):
    with open(fn, encoding='utf8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            errors = True
            print('Error loading JSON', e)
            traceback.print_exc()
            continue
    
    file = os.path.basename(os.path.normpath(fn))
    if file == 'as_monsters.json':
        for k, v in data.items():
            if not isinstance(k, str):
                error(file, k, 'Monster name has to be a string')
            
            keys = {
                'hp': int,
                'pdef': (float, int),
                'mdef': (float, int),
                'dipl': (float, int),
                'image': URL,
                'boss': bool,
                'miniboss': dict,
                'color': str
            }
            check(file, k, v, keys)

    elif file == 'attribs.json':
        for k, v in data.items():
            if not isinstance(k, str):
                error(file, k, 'Attribute name has to be a string')
            
            for x in v:
                if not isinstance(x, (float, int)):
                    error(file, k, 'Attribute values have to be floats')

    # elif file == 'equipment.json':
    # TODO: add more

# exit code
# ignore warnings for now
# TODO: add a cmd line argument to control whether to ignore warnings
exit(errors)
