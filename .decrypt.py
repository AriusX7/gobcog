import os
from glob import iglob

from cryptography.fernet import Fernet


print('This script decrypts all encrypted datafiles from adventure/data/*/.enc.')
print('Do not run this unless absolutely necessary.')
print('When running the bot, the bot will already decrypt datafiles to memory.')
print('This is only necessary to edit the datafiles.')
confirm = input('Reply with Y to confirm: ')

if confirm != 'Y':
    exit(0)


for i in iglob('adventure/data/*/*.enc'):
    theme = os.path.normpath(i).split(os.sep)[2]
    name = os.path.split(i)[1]

    with open(f'adventure/data/{theme}/key.key', 'rb') as f:
        key_data = f.read()
        key = Fernet(key_data)

    with open(i, 'rb') as f:
        decrypted_data = key.decrypt(f.read())

    # Create decrypted folder if doesn't exist.
    if not os.path.isdir('decrypted'):
        os.mkdir('decrypted')

    # create theme folder if doesnt exist
    if not os.path.isdir(f'decrypted/{theme}'):
        os.mkdir(f'decrypted/{theme}')

    # save key as well
    if not os.path.isfile(f'decrypted/{theme}/key.key'):
        with open(f'decrypted/{theme}/key.key', 'wb') as f:
            f.write(key_data)

    with open(f'decrypted/{theme}/{name[:-4]}.json', 'wb') as f:
        f.write(decrypted_data)

    print(name)
