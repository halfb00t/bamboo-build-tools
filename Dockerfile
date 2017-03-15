FROM python:2.7-alpine
MAINTAINER Andrew S. <halfb00t@gmail.com>

RUN apk add --no-cache libxslt libxml2
RUN apk add --no-cache --virtual .build-deps build-base git libxslt-dev libxml2-dev \
    && cd /tmp && git clone https://github.com/halfb00t/bamboo-build-tools.git . \
    && python setup.py build && python setup.py install \
    && cd / && rm -rf /tmp/* \
    && apk del .build-deps

ENV APP_HOME /app
WORKDIR $APP_HOME
