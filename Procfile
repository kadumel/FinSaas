release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
web: gunicorn finsaas.wsgi --bind 0.0.0.0:$PORT
