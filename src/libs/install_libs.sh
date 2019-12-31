mount -o remount,rw /
OS_DIST=$(awk -F= '$1=="ID" { print $2 ;}' /etc/os-release)
python /opt/openmotics/python/libs/$OS_DIST/pip-19.3.1-py2.py3-none-any.whl/pip install --no-index /opt/openmotics/python/libs/$OS_DIST/*.whl
mount -o remount,ro /
