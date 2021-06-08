import os
from glob import iglob

from cryptography.fernet import Fernet


print('This script encrypts all decrypted datafiles from decrypted/*/*.json.')
print('This will overwrite all pre-existing datafiles saved in adventure/data.')
print('This is only necessary to edit the datafiles.')
print('IMPORTANT: Take extra caution not to commit decrypted datafiles.')
confirm = input('Reply with Y to confirm: ')

if confirm != 'Y':
    exit(0)


for i in iglob('decrypted/*/*.json'):
    theme = os.path.normpath(i).split(os.sep)[1]
    name = os.path.split(i)[1]

    with open(f'decrypted/{theme}/key.key', 'rb') as f:
        key_data = f.read()
        key = Fernet(key_data)

    with open(i, 'rb') as f:
        encrypted_data = key.encrypt(f.read())

    # create theme folder if doesnt exist
    if not os.path.isdir(f'adventure/data/{theme}'):
        os.mkdir(f'adventure/data/{theme}')

    # save key as well
    if not os.path.isfile(f'adventure/data/{theme}/key.key'):
        with open(f'adventure/data/{theme}/key.key', 'wb') as f:
            f.write(key_data)

    with open(f'adventure/data/{theme}/{name[:-5]}.enc', 'wb') as f:
        f.write(encrypted_data)

    print(name)
