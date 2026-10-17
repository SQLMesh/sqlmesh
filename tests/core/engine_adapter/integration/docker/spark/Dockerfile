FROM bitnamilegacy/spark:3.5.2
USER root
RUN install_packages curl
USER 1001
RUN curl --fail --show-error --location --retry 3 --retry-delay 5 --retry-all-errors \
      -o /opt/bitnami/spark/jars/postgresql-42.5.0.jar \
      https://jdbc.postgresql.org/download/postgresql-42.5.0.jar
RUN curl --fail --show-error --location --retry 3 --retry-delay 5 --retry-all-errors \
      -o /opt/bitnami/spark/jars/aws-java-sdk-bundle-1.11.704.jar \
      https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.11.704/aws-java-sdk-bundle-1.11.704.jar
RUN curl --fail --show-error --location --retry 3 --retry-delay 5 --retry-all-errors \
      -o /opt/bitnami/spark/jars/iceberg-spark-runtime-3.5_2.12-1.4.1.jar \
      https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/1.4.1/iceberg-spark-runtime-3.5_2.12-1.4.1.jar
