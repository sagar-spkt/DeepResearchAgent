# CodeBuild's curated image supplies its build agent and the pinned Kubernetes
# client used by the project. Terraform and boto3 match repository pins.
FROM public.ecr.aws/codebuild/amazonlinux-x86_64-standard:5.0

COPY --from=hashicorp/terraform:1.17.1 /bin/terraform /usr/local/bin/terraform

RUN python3.12 -m pip install --no-cache-dir boto3==1.43.119 \
    && terraform version | grep -Fx 'Terraform v1.17.1' \
    && kubectl version --client -o yaml | grep -F 'gitVersion: v1.37.1' \
    && aws --version \
    && python3.12 --version
