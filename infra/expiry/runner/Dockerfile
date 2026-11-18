FROM hashicorp/terraform:1.17.1@sha256:a5d5a5a892c98f60fcbfa81eac2a40fc19e7b3fa7719bccae10ff6111d22524a AS terraform
FROM registry.k8s.io/kubectl:v1.37.1@sha256:b7cab618e281b1ee7484e7b706a96e2135fbb6e072c2a573a7dab4e87d7f2385 AS kubectl
FROM public.ecr.aws/aws-cli/aws-cli:2.40.3@sha256:33a16c53ea442e918b58df67301ad76a8fb57082c39f7e78753b898c3f3e87c2 AS awscli
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl
COPY --from=awscli /usr/local/aws-cli /usr/local/aws-cli
RUN ln -s /usr/local/aws-cli/v2/current/bin/aws /usr/local/bin/aws \
    && python3 -m pip install --no-cache-dir boto3==1.43.119 \
    && terraform version | grep -Fx 'Terraform v1.17.1' \
    && kubectl version --client -o yaml | grep -F 'gitVersion: v1.37.1' \
    && aws --version \
    && python3 -c 'import boto3'
