web: gunicorn wateronmars.wsgi:application -b 0.0.0.0:$PORT
celeryd: python manage.py celeryd -E -B --loglevel=INFO

